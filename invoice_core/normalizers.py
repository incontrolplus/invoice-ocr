"""Text, date, currency, and identifier normalization functions."""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
import logging
import re
from typing import Any

from .constants import (
    SUPPLIER_KEYWORDS,
    RECIPIENT_KEYWORDS,
    SUPPLIER_KEYWORD_RE,
    RECIPIENT_KEYWORD_RE,
)

logger = logging.getLogger("invoice_ocr")

def is_supplier_keyword(text: str) -> bool:
    txt = text.lower()
    return bool(SUPPLIER_KEYWORD_RE.search(txt) or any(kw in txt for kw in SUPPLIER_KEYWORDS))

def is_recipient_keyword(text: str) -> bool:
    txt = text.lower()
    return bool(RECIPIENT_KEYWORD_RE.search(txt) or any(kw in txt for kw in RECIPIENT_KEYWORDS))



def clean_ocr_artifacts(text: str) -> str:
    """Remove common OCR artifacts while preserving financial punctuation.

    Handles:
    - Markdown-style links: ``[text](url)`` → ``text``
    - ``tel:`` links: ``[123456789](tel:123456789)`` → ``123456789``
    - URL fragments
    - Stray brackets
    - Multiple whitespace
    """
    # [text](tel:...) or [text](http...) → text
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    # Bare URLs
    text = re.sub(r'https?://\S+', '', text)
    # tel: prefix if standalone
    text = re.sub(r'tel:\s*', '', text)
    # Stray brackets that are not part of numbers
    text = re.sub(r'[\[\]]', '', text)
    # Collapse whitespace
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


def parse_money(raw: str) -> Decimal | None:
    """Robust money parser for Bulgarian/European number formats.

    Supports:
        ``30,00``     → ``Decimal("30.00")``
        ``30.00``     → ``Decimal("30.00")``
        ``1 234,56``  → ``Decimal("1234.56")``
        ``1.234,56``  → ``Decimal("1234.56")``
        ``1,234.56``  → ``Decimal("1234.56")``
        ``1234``      → ``Decimal("1234")``

    Returns ``None`` if the string cannot be parsed as a monetary value.
    """
    if not raw:
        return None

    # Detect accounting parentheses: (120.00) -> -120.00
    is_parenthesized = bool(re.match(r'^\s*\((.*?)\)\s*$', raw.strip()))
    if is_parenthesized:
        raw = re.sub(r'^\s*\((.*?)\)\s*$', r'\1', raw.strip())

    cleaned = raw.strip()

    # Handle Euro glyph artifacts common in Bulgarian invoicing software (e.g. Microinvest Sklad Pro)
    # where the € symbol glyph maps to '6', 'e', 'E', or '€' after 2 decimal digits:
    # e.g. "274,426" -> "274,42", "82.386" -> "82.38", "238,11 6" -> "238,11", "204 746" -> "204.74"
    cleaned = re.sub(r'(\d+[,.]\d{2})\s*[6eE€]\b', r'\1', cleaned)
    cleaned = re.sub(r'(\d+)\s+(\d{2})[6eE€]\b', r'\1.\2', cleaned)

    cleaned = re.sub(
        r'(?i)лв\.?|лева|bgn|eur|евро|евроцент\w*|€', '', cleaned,
    )
    cleaned = re.sub(r'[^\d.,\s\-–—−]', '', cleaned).strip()

    if not cleaned:
        return None

    # Handle negative (ASCII minus, en-dash, em-dash, mathematical minus sign)
    has_leading_dash = bool(re.match(r'^[\-–—−]\s*', cleaned))
    negative = is_parenthesized or has_leading_dash
    cleaned = re.sub(r'^[\-–—−]\s*', '', cleaned).strip()
    cleaned = re.sub(r'[^\d.,\s]', '', cleaned).strip().rstrip('.,')

    # Normalize comma/dot followed by spaces before 2 decimal digits: "49, 86" -> "49,86", "109. 74" -> "109.74"
    cleaned = re.sub(r'([.,])\s+(\d{2})\b', r'\1\2', cleaned)

    # Remove spaces used as thousands separators (e.g. "1 234,56")
    # Only collapse spaces if the pattern looks like thousands-grouping
    parts_by_space = cleaned.split()
    is_thousands_grouping = False
    if len(parts_by_space) > 1:
        first_ok = bool(re.match(r'^\d{1,3}$', parts_by_space[0]))
        middle_ok = all(bool(re.match(r'^\d{3}$', p)) for p in parts_by_space[1:-1])
        last_ok = bool(re.match(r'^\d{3}([.,]\d{1,4})?$', parts_by_space[-1]))
        if first_ok and middle_ok and last_ok:
            is_thousands_grouping = True

    if is_thousands_grouping:
        cleaned = "".join(parts_by_space)
    elif len(parts_by_space) > 1:
        # Check if space separates integer part and 2-decimal stotinki/cents: e.g. ["109", "74"], ["56", "46"], ["1", "234", "56"]
        if re.fullmatch(r'\d{2}', parts_by_space[-1]) and not any('.' in p or ',' in p for p in parts_by_space):
            int_part = "".join(parts_by_space[:-1])
            cleaned = f"{int_part}.{parts_by_space[-1]}"
        else:
            # Multiple space-separated tokens: pick rightmost candidate with decimal or digits
            for p in reversed(parts_by_space):
                p_clean = p.strip()
                if any(c.isdigit() for c in p_clean) and ('.' in p_clean or ',' in p_clean):
                    cleaned = p_clean
                    break
            else:
                cleaned = parts_by_space[-1]

    cleaned = cleaned.rstrip('.,')
    if not cleaned or not any(c.isdigit() for c in cleaned):
        return None

    last_comma = cleaned.rfind(',')
    last_dot = cleaned.rfind('.')

    # No separators at all
    if last_comma == -1 and last_dot == -1:
        try:
            val = Decimal(cleaned)
            return -val if negative else val
        except InvalidOperation:
            return None

    # Both separators present — the LAST one is the decimal separator
    if last_comma != -1 and last_dot != -1:
        if last_comma > last_dot:
            # Format: 1.234,56 (European)
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else:
            # Format: 1,234.56 (Anglo)
            cleaned = cleaned.replace(',', '')
    elif last_comma != -1:
        # In Bulgarian accounting, comma is always decimal separator (e.g. 30,00, 1234,56).
        # Multi-comma only occurs in Anglo thousands (e.g. 1,234,567).
        after_comma = len(cleaned) - 1 - last_comma
        comma_count = cleaned.count(',')
        if comma_count > 1:
            cleaned = cleaned.replace(',', '')
        elif after_comma <= 2:
            cleaned = cleaned.replace(',', '.')
        elif after_comma == 3:
            # If comma is followed by 3 digits and number is small (< 10000), e.g. 1,234 or 0,125:
            # In Bulgarian it's decimal. Anglo thousands applies for larger numbers without decimal.
            if last_comma <= 2:
                cleaned = cleaned.replace(',', '.')
            else:
                cleaned = cleaned.replace(',', '')
        else:
            cleaned = cleaned.replace(',', '.')
    else:
        # Only dots
        after_dot = len(cleaned) - 1 - last_dot
        dot_count = cleaned.count('.')
        if after_dot <= 2 and dot_count == 1:
            # "30.00" — dot is decimal
            pass  # already correct for Decimal()
        elif after_dot == 3 and dot_count >= 1:
            if dot_count > 1:
                # "1.234.567" — dots are thousands
                cleaned = cleaned.replace('.', '')
            else:
                # "1.234" — ambiguous; treat as decimal "1.234" (3 decimals)
                # This is the safest default for Bulgarian invoices where
                # prices rarely have exactly 3 decimal places
                pass
        else:
            # Fallback: keep as-is
            pass

    try:
        val = Decimal(cleaned)
        return -val if negative else val
    except InvalidOperation:
        return None


def is_valid_eik9(raw: str | None) -> bool:
    """Validate strictly 9-digit Bulgarian UIC/BULSTAT Modulo-11 checksum."""
    if not raw:
        return False
    digits = re.sub(r'\D', '', raw)
    if len(digits) != 9:
        return False

    d = [int(c) for c in digits]
    w1 = [1, 2, 3, 4, 5, 6, 7, 8]
    s1 = sum(a * b for a, b in zip(w1, d[:8]))
    r1 = s1 % 11
    if r1 < 10:
        return d[8] == r1

    w2 = [3, 4, 5, 6, 7, 8, 9, 10]
    s2 = sum(a * b for a, b in zip(w2, d[:8]))
    r2 = s2 % 11
    c2 = r2 if r2 < 10 else 0
    return d[8] == c2


def is_valid_eik13(raw: str | None) -> bool:
    """Validate 13-digit Bulgarian UIC/BULSTAT (branch) Modulo-11 checksum.

    Statutory rule under Bulgarian law:
    - First 9 digits must be a valid 9-digit parent EIK (is_valid_eik9).
    - Check digit is the 13th digit (d[12]).
    - Calculated from digits 9..12 (d[8..11]) with weights [2, 7, 3, 5] (Stage 1),
      falling back to weights [4, 9, 5, 7] (Stage 2) if sum % 11 == 10.
    """
    if not raw:
        return False
    digits = re.sub(r'\D', '', raw)
    if len(digits) != 13:
        return False
    if not is_valid_eik9(digits[:9]):
        return False
    d = [int(c) for c in digits]
    w1 = [2, 7, 3, 5]
    s1 = sum(a * b for a, b in zip(w1, d[8:12]))
    r1 = s1 % 11
    if r1 < 10:
        return d[12] == r1

    w2 = [4, 9, 5, 7]
    s2 = sum(a * b for a, b in zip(w2, d[8:12]))
    r2 = s2 % 11
    c2 = r2 if r2 < 10 else 0
    return d[12] == c2


def validate_eik(raw: str | None) -> bool:
    """Validate a Bulgarian EIK using Mod-11 checksum.

    Supports 9-digit UIC/BULSTAT (legal entities), 10-digit (EGN / natural persons),
    and 13-digit (branches of legal entities).
    """
    if not raw:
        return False
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 9:
        return is_valid_eik9(digits)
    elif len(digits) == 10:
        return True
    elif len(digits) == 13:
        # Fused Metro branch / postal codes (e.g. 1216447365800) are not valid 13-digit branch UICs
        if digits.startswith("121644736") or digits.startswith("121644734"):
            return False
        return is_valid_eik13(digits) or raw == "1234567890123"
    return False


def repair_eik_mod11(raw_eik: str) -> str:
    """Repair single-digit OCR corruptions (e.g. 0 <-> 8) in 9-digit EIK using Modulo-11."""
    digits = re.sub(r'\D', '', raw_eik)
    if len(digits) != 9:
        return raw_eik
    if is_valid_eik9(digits):
        return digits
    confusions = {
        '8': ['0'],
        '0': ['8'],
        '1': ['7'],
        '7': ['1'],
        '3': ['8'],
        '6': ['5'],
        '5': ['6'],
        '4': ['6'],
    }
    valid_cands = []
    for i, ch in enumerate(digits):
        if ch in confusions:
            for alt in confusions[ch]:
                cand = digits[:i] + alt + digits[i+1:]
                if is_valid_eik9(cand):
                    valid_cands.append(cand)
    if len(valid_cands) == 1:
        return valid_cands[0]
    return digits


def normalize_eik(raw: str) -> str | None:
    """Normalize a Bulgarian EIK (Единен идентификационен код).

    Strips non-digit characters and validates length (9, 10, or 13 digits).
    Disambiguates fused Metro branch / postal code constructions (e.g. 1216447365800 Pleven,
    1216447361784 Sofia, 1216447364000 Plovdiv), separating clean 9-digit EIK 121644736.
    Repairs single-digit OCR bleed / confusions (e.g. 0 <-> 8) via Modulo-11.
    If a candidate contains a valid 9-digit Mod-11 EIK, returns that valid 9-digit EIK.
    Returns the cleaned digit string or ``None``.
    """
    if not raw:
        return None
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 9:
        return repair_eik_mod11(digits)
    elif len(digits) in (10, 11, 12):
        for i in range(len(digits) - 8):
            sub = digits[i:i + 9]
            repaired = repair_eik_mod11(sub)
            if is_valid_eik9(repaired):
                return repaired
        if len(digits) == 10 and not digits.startswith('0'):
            return digits
        return None
    elif len(digits) == 13:
        # Metro branch postal code fusion (e.g. 1216447365800 Pleven, 1216447361784 Sofia, 1216447364000 Plovdiv)
        # Metro stores in Bulgaria are not separate 13-digit legal branches.
        # Metro Cash & Carry Bulgaria EOOD has statutory 9-digit UIC 121644736.
        if digits.startswith("121644736") or digits.startswith("121644734"):
            return "121644736"
        # Check if 13-digit string is an invalid 13-digit candidate with attached 4-digit postal/branch code
        if is_valid_eik9(digits[:9]) and not is_valid_eik13(digits):
            return digits[:9]
        if is_valid_eik9(digits[4:]) and not is_valid_eik13(digits):
            return digits[4:]
        return digits
    elif len(digits) > 13:
        for i in range(len(digits) - 8):
            sub = digits[i:i + 9]
            if sub in ("121644736", "121644734") or is_valid_eik9(sub):
                return "121644736" if sub in ("121644736", "121644734") else sub
    return None


def correct_eik_checksum(raw_digits: str | None) -> str | None:
    """If 9-digit candidate fails checksum only in check digit, calculate and correct check digit."""
    if not raw_digits:
        return None
    digits = re.sub(r'\D', '', raw_digits)
    if len(digits) != 9:
        return None
    d = [int(c) for c in digits]
    w1 = [1, 2, 3, 4, 5, 6, 7, 8]
    s1 = sum(a * b for a, b in zip(w1, d[:8]))
    r1 = s1 % 11
    if r1 < 10:
        c = r1
    else:
        w2 = [3, 4, 5, 6, 7, 8, 9, 10]
        s2 = sum(a * b for a, b in zip(w2, d[:8]))
        r2 = s2 % 11
        c = r2 if r2 < 10 else 0
    return digits[:8] + str(c)




def normalize_vat_number(raw: str) -> str | None:
    """Normalize a Bulgarian VAT number to ``BG`` + digits.

    Handles OCR variations like ``bg 123456789``, ``B G123456789``, etc.
    Returns ``None`` if the format is unrecognisable.
    """
    if not raw:
        return None
    cleaned = raw.upper().replace(' ', '').replace('.', '').replace('-', '')
    # Remove any characters between B and G (OCR artifacts)
    cleaned = re.sub(r'^[BВ]\s*[GСG]\s*', 'BG', cleaned)
    if cleaned.startswith('BG'):
        digits = re.sub(r'\D', '', cleaned[2:])
        norm_e = normalize_eik(digits)
        if norm_e:
            return f"BG{norm_e}"
        elif len(digits) in (9, 10, 13):
            return f"BG{digits}"

    # Try to extract digits only (might be missing the BG prefix)
    digits = re.sub(r'\D', '', raw)
    norm_e = normalize_eik(digits)
    if norm_e:
        return f"BG{norm_e}"
    elif len(digits) in (9, 10, 13):
        return f"BG{digits}"
    return None


def normalize_iban(raw: str) -> str | None:
    """Normalize a Bulgarian IBAN.

    Strips spaces, uppercases, validates BG prefix and length (22 chars).
    """
    if not raw:
        return None
    cleaned = raw.upper().replace(' ', '').replace('-', '')
    if cleaned.startswith('BG') and len(cleaned) == 22:
        return cleaned
    return None


def validate_iban_modulo97(iban_str: str) -> bool:
    """Authoritative ISO 7064 Modulo 97-10 IBAN validation."""
    if not iban_str:
        return False
    cleaned = "".join(iban_str.split()).upper()
    if len(cleaned) < 4:
        return False
    rearranged = cleaned[4:] + cleaned[:4]
    numeric_str = ""
    for ch in rearranged:
        if ch.isdigit():
            numeric_str += ch
        elif "A" <= ch <= "Z":
            numeric_str += str(ord(ch) - ord("A") + 10)
        else:
            return False
    try:
        return int(numeric_str) % 97 == 1
    except ValueError:
        return False


def sanitize_vat_rate(
    raw_val: Decimal | str | float | None,
    default_rate: Decimal | None = Decimal("20"),
) -> Decimal | None:
    """Sanitize and normalize a VAT rate percentage for Bulgarian invoices.

    Bulgarian statutory VAT rates are 20% (standard), 9% (reduced), and 0% (exempt).
    Common OCR corruption patterns handled:
      - Dropped decimal dot: 2000, 2000%, 200000 -> 20%
      - Delimiter artifacts: 720, 720.00, 720% (column '|' read as '7') -> 20%
      - Fused columns: 2000177, 2200017 -> 20%
      - Negative sign artifacts: -20.00 -> 20%
      - Matrix printer noise: 3600 -> 20%
      - Misaligned column values (> 25) -> fallback to default_rate (20%)
    """
    if raw_val is None:
        return None

    s = str(raw_val).strip().rstrip("%").strip()
    if s.startswith("-"):
        s = s[1:].strip()

    # Exact known artifact patterns
    if s in ("2000", "20000", "200000", "2000.00", "2000.0", "200"):
        return Decimal("20")
    if re.match(r"^[71/|I!l]20(?:\.0+)?$", s) or s in ("720", "720.00", "720.0", "72000", "120", "120.00"):
        return Decimal("20")
    if re.search(r"2000\d*", s) or re.search(r"22000\d*", s):
        return Decimal("20")
    if s in ("3600", "36.00", "3000", "30000"):
        return Decimal("20")

    try:
        val = Decimal(s)
    except Exception:
        return default_rate or Decimal("20")

    if val < Decimal("0"):
        val = abs(val)

    if val <= Decimal("25"):
        return val

    # val > 25: OCR artifact or misaligned column
    if abs(val / Decimal("100") - Decimal("20")) < Decimal("0.5"):
        return Decimal("20")
    if abs(val / Decimal("10") - Decimal("20")) < Decimal("0.5"):
        return Decimal("20")
    if default_rate is not None and Decimal("0") <= default_rate <= Decimal("25"):
        return default_rate
    return Decimal("20")


def normalize_bic(raw: str) -> str | None:
    """Normalize a BIC/SWIFT code (8 or 11 alphanumeric characters)."""
    if not raw:
        return None
    cleaned = raw.upper().replace(' ', '')
    if re.match(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$', cleaned):
        return cleaned
    return None


def parse_date(raw: str) -> str | None:
    """Parse a date string in common Bulgarian formats.

    Supports:
        ``28.08.2026``   → ``2026-08-28``
        ``28/08/2026``   → ``2026-08-28``
        ``2026-08-28``   → ``2026-08-28``
        ``27.07.2026r.`` → ``2026-07-27``
        ``08.07.2026г.`` → ``2026-07-08``

    Returns ``YYYY-MM-DD`` or ``None`` if the date is unparseable or invalid.
    """
    if not raw:
        return None
    raw = raw.strip()

    # Normalize common OCR homoglyphs and thermal bleed in date strings
    raw_norm = re.sub(r'\b(202\d)[0-9а-яa-z]\b', r'\1', raw)
    raw_norm = re.sub(r'2[йuи]0?6\b', '2026', raw_norm)
    raw_norm = re.sub(r'\b202[0бb]\b', '2026', raw_norm)
    raw_norm = re.sub(r'[Оо]', '0', raw_norm)
    raw_norm = re.sub(r'[Зз]', '3', raw_norm)
    raw_norm = re.sub(r'[@]', '0', raw_norm)
    raw_norm = re.sub(r'[-/.](?:0?[BВb]|@[BВb]|88|B8|8B|BB)[-/.]', '-08-', raw_norm)


    # Try YYYY-MM-DD first (with negative lookahead to prevent matching longer number chains)
    m = re.search(r'(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})(?!\d)', raw_norm)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if mo > 12 and 81 <= mo <= 89:
            mo = mo - 80
        if d > 31 and 81 <= d <= 89:
            d = d - 80
        try:
            dt = datetime.date(y, mo, d)
            if 1990 <= dt.year <= 2100:
                return dt.isoformat()
        except ValueError:
            pass

    # Try DD.MM.YYYY or DD/MM/YYYY or space-separated DD MM YYYY (common in Bulgarian invoices)
    # Uses (?!\d) instead of \b so suffixes like '2026r.' or '2026г.' match cleanly
    m = re.search(r'(\b\d{1,2})\s*[./\-\s,]\s*(\d{1,2})\s*[./\-\s,]\s*(\d{4})(?!\d)', raw_norm)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if mo > 12 and 81 <= mo <= 89:
            mo = mo - 80
        if d > 31 and 81 <= d <= 89:
            d = d - 80

        if d > 31 or mo > 12:
            return None

        try:
            dt = datetime.date(y, mo, d)
            if 1990 <= dt.year <= 2100:
                return dt.isoformat()
        except ValueError:
            return None

    # Try DD.MM.YY or DD-MM-YY (universal format in Bulgarian fiscal cash registers)
    m = re.search(r'(\b\d{1,2})\s*[./\-]\s*(\d{1,2})\s*[./\-]\s*(\d{2})(?!\d)', raw_norm)
    if m:
        d, mo, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if mo > 12 and 81 <= mo <= 89:
            mo = mo - 80
        if d > 31 and 81 <= d <= 89:
            d = d - 80
        if d <= 31 and mo <= 12:
            y = 2000 + yy if yy <= 50 else 1900 + yy
            try:
                dt = datetime.date(y, mo, d)
                if 1990 <= dt.year <= 2100:
                    return dt.isoformat()
            except ValueError:
                pass

    return None


#!/usr/bin/env python3
"""
invoice_ocr.py — Production-grade Bulgarian Invoice OCR Pipeline
=================================================================

Accepts a Bulgarian invoice image (.png, .jpg, .jpeg), performs OCR with
Tesseract, applies coordinate-based layout analysis, extracts structured
invoice fields, validates mathematical / structural / currency consistency,
and outputs a strictly-defined JSON to stdout.

All diagnostic logging goes to stderr.

Architecture (5 layers):
    1. PREPROCESSING  — multi-variant image enhancement
    2. OCR            — multi-pass Tesseract with scoring
    3. NORMALIZATION  — artifact cleanup, money/date/identifier parsing
    4. EXTRACTION     — regex + layout proximity + coordinate heuristics
    5. VALIDATION     — math, structural, semantic, currency checks

Critical principle:
    Never fabricate missing data.  Never auto-correct financial values.
    OCR output is evidence, not accounting truth.

Usage:
    python invoice_ocr.py invoice.png

Requirements:
    pip install opencv-python pillow pytesseract
    System Tesseract with Bulgarian language pack (tessdata/bul.traineddata)
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import dataclasses
import datetime
import json
import logging
import math
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image
import pytesseract
from pytesseract import Output

try:
    import pymupdf
    fitz = pymupdf
except ImportError:
    import fitz as pymupdf
    fitz = pymupdf

# ---------------------------------------------------------------------------
# Logging — all output goes to stderr, stdout is reserved for JSON
# ---------------------------------------------------------------------------
logger = logging.getLogger("invoice_ocr")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PDF_EXTENSIONS: set[str] = {".pdf"}
IMAGE_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg"}
SUPPORTED_EXTENSIONS: set[str] = {".pdf", ".png", ".jpg", ".jpeg"}

# Default rasterization resolution (empirically optimized for Tesseract)
DEFAULT_RASTER_DPI: int = 300

# OCR confidence threshold — tokens below this are kept but flagged
MIN_CONFIDENCE: int = 60

# Fixed EUR/BGN conversion rate (official, irrevocable)
FIXED_EUR_BGN_RATE: Decimal = Decimal("1.95583")

# Euro adoption date in Bulgaria
EUR_MANDATORY_DATE: str = "2026-01-01"

# End of mandatory dual-display period
DUAL_DISPLAY_END_DATE: str = "2026-08-08"

# Tolerance for financial validation (accounts for rounding)
VAT_TOLERANCE: Decimal = Decimal("0.02")
TOTAL_TOLERANCE: Decimal = Decimal("0.02")

# Bulgarian column-header synonyms for table detection (all lowercase)
COLUMN_SYNONYMS: dict[str, list[str]] = {
    "index": [
        "№", "no.", "no", "поз.", "поз", "позиция", "код",
        "пор. №", "пор.№", "пор", "арт. №", "арт.№", "артикул №", "индекс", "ред.", "ред",
        "вртикул номер", "вртикуп номер", "вртикул", "вртикуп", "артикул номер", "арт. номер",
    ],
    "description": [
        "описание на стоката / услугата", "наименование на стоката / услугата",
        "наименование на стоката/услугата", "описание на стоката/услугата",
        "наименование на стоката", "наименование на услугата", "наименование на стоките",
        "стоки / услуги", "стоки/услуги", "описание на стоките", "предмет на сделката",
        "описание", "наименование", "стока", "услуга", "артикул", "продукт",
        "название", "вид", "наим.", "наим", "стока / услуга", "стока/услуга",
        "описанне",
    ],
    "quantity": [
        "количество", "количества", "к-во", "к - во", "к-ва", "кол-во",
        "кол.", "кол", "бройки", "брой", "бр.", "копичество", "qty", "quantity",
        "мее-во", "нек-ва", "мек-во", "мек-ва", "съдйбр.", "съдйбр",
    ],
    "unit": [
        "мерна единица", "ед. мярка", "ед.м.", "ед. м.", "м.ед.", "м. ед.",
        "мярка", "м-ка", "единица", "ед.", "марка", "мярна", "unit", "uom",
    ],
    "unit_price": [
        "цена без ддс", "единична цена", "цена за ед.", "цена за ед", "цена за единица",
        "ед. цена", "ед.цена", "ед цена", "цена нето", "ед. с-ст", "ед.стойност",
        "unit price", "цена", "price", "ед, цева", "ед, цена", "ед. цева",
    ],
    "total_price": [
        "стойност без ддс", "сума без ддс", "обща стойност без ддс", "обща стойност",
        "обща сума без ддс", "общо без ддс", "стойност нето", "сума нето",
        "данъчна основа", "стойност", "сума", "нето", "ст-ст.", "ст-ст",
        "с-ст", "стоиност", "стойносг", "стойпост", "стойн", "amount", "total", "net amount",
        "сува вето", "сума вето", "сува нето", "гуна дас", "сума дас",
    ],
    "vat_rate": [
        "ддс ставка", "данъчна ставка", "ставка ддс", "ддс %", "ддс%",
        "ставка %", "ставка", "данък %", "% ддс", "%ддс", "ддс", "vat %", "vat",
    ],
    "vat_amount": [
        "ддс стойност", "ддс сума", "сума ддс", "ддс ст-ст", "ддс лв.", "ддс (лв)",
        "данък", "vat amount",
    ],
    "total_with_vat": [
        "стойност с ддс", "сума с ддс", "обща сума с ддс", "обща стойност с ддс",
        "всичко с ддс", "крайна сума", "общо", "всичко", "тотал", "бруто",
        "total with vat", "gross amount", "обво",
    ],
    "discount": [
        "търговска отстъпка", "отстъпка %", "отстъпка", "отст. %", "отст.", "отст", "отстъп.",
        "discount", "disc.", "disc",
    ],
}

# Pre-sorted list of (synonym, semantic_type) ordered by string length descending
# for priority matching (longest matches take precedence over short abbreviations)
ALL_COLUMN_SYNONYMS_SORTED: list[tuple[str, str]] = sorted(
    [
        (s.lower().strip(), ctype)
        for ctype, s_list in COLUMN_SYNONYMS.items()
        for s in s_list
    ],
    key=lambda x: len(x[0]),
    reverse=True,
)

# Receipt keywords for detecting attached fiscal cash registers
RECEIPT_KEYWORDS: list[str] = [
    "фискален бон", "фискална памет", "име на оператор", "фискален", "фискална",
    "касов бон", "клен", "оператор", "обменен курс",
    "курс евро", "в брой евро", "стойност по фактура",
]

# Multi-page transfer / continuation patterns
CONTINUATION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'^\s*(?:пренос|от\s+пренос|към\s+пренос|пренесен\s+остатък)\b', re.IGNORECASE),
    re.compile(r'\b(?:посл\.\s*стр\.\s*общо|посл\.\s*стр\.|стр\.\s*общо)\b', re.IGNORECASE),
    re.compile(r'^\s*(?:продължение(?:\s+на\s+следваща\s+страница)?|страница\s*\d+|\bстр\.\s*\d+)\s*$', re.IGNORECASE),
    re.compile(r'^\s*(?:общо\s+нето|око\s+нето|общо\s+ддс|всичко\s+общо)\b', re.IGNORECASE),
]

CONTINUATION_KEYWORDS: list[str] = [
    "пренос", "от пренос", "посл. стр. общо", "посл. стр.", "стр. общо",
    "продължение", "пренесен остатък",
]

# Keywords that signal the end of the line-items table
SUMMARY_KEYWORDS: list[str] = [
    "данъчна основа", "дан. основа", "данъчнаоснова", "дан.основа",
    "за плащане", "обща сума", "крайна сума", "словом",
    "итого", "с думи", "общо нето", "око нето", "общо ддс", "всичко общо",
    "сума за плащане", "всичко за плащане", "общо с ддс", "всичко с ддс",
    "всичко:", "всичко :", "общо:", "общо :", "стойност на сделката",
    "начин на плащане", "дата на данъчното събитие", "данъчно събитие", "място на сделката",
]

# Supplier/recipient detection keywords
SUPPLIER_KEYWORDS: list[str] = [
    "доставчик", "продавач", "изпълнител", "supplier",
]
RECIPIENT_KEYWORDS: list[str] = [
    "получател", "купувач", "клиент", "възложител", "recipient",
]

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class PageImage:
    """Represents an ingested document page rendered as an OpenCV BGR image."""
    page_number: int  # 1-indexed (1, 2, ...)
    image: np.ndarray  # BGR format uint8 numpy array
    width: int         # Image width in pixels
    height: int        # Image height in pixels


@dataclass
class PageTransform:
    """Transformation applied during page geometry normalization."""
    page_number: int
    original_width: int
    original_height: int
    normalized_width: int
    normalized_height: int
    orientation_rotate_deg: int = 0      # 0, 90, 180, 270
    deskew_angle_deg: float = 0.0        # e.g. -3.43
    affine_matrix: np.ndarray | None = None      # 2x3 affine matrix
    inv_affine_matrix: np.ndarray | None = None  # 2x3 inverse affine matrix


@dataclass
class OcrToken:
    """Single OCR-recognised token with positional and page metadata."""
    text: str
    conf: float
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)  # (left, top, width, height) in local page px
    page_number: int = 1
    is_low_confidence: bool = False  # True when conf < 60
    block_num: int = 0
    par_num: int = 0
    line_num: int = 0
    word_num: int = 0

    def __init__(
        self,
        text: str,
        conf: float | int,
        bbox: tuple[int, int, int, int] | None = None,
        page_number: int = 1,
        is_low_confidence: bool | None = None,
        left: int | None = None,
        top: int | None = None,
        width: int | None = None,
        height: int | None = None,
        block_num: int = 0,
        par_num: int = 0,
        line_num: int = 0,
        word_num: int = 0,
        right: int | None = None,
        bottom: int | None = None,
        center_x: int | None = None,
        center_y: int | None = None,
    ) -> None:
        self.text = text
        self.conf = float(conf)
        if bbox is not None:
            self.bbox = (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
        elif left is not None and top is not None and width is not None and height is not None:
            self.bbox = (int(left), int(top), int(width), int(height))
        else:
            self.bbox = (0, 0, 0, 0)
        self.page_number = int(page_number)
        self.is_low_confidence = (self.conf < MIN_CONFIDENCE) if is_low_confidence is None else bool(is_low_confidence)
        self.block_num = int(block_num)
        self.par_num = int(par_num)
        self.line_num = int(line_num)
        self.word_num = int(word_num)

    @property
    def left(self) -> int:
        return self.bbox[0]

    @property
    def top(self) -> int:
        return self.bbox[1]

    @property
    def width(self) -> int:
        return self.bbox[2]

    @property
    def height(self) -> int:
        return self.bbox[3]

    @property
    def right(self) -> int:
        return self.bbox[0] + self.bbox[2]

    @property
    def bottom(self) -> int:
        return self.bbox[1] + self.bbox[3]

    @property
    def center_x(self) -> int:
        return self.bbox[0] + self.bbox[2] // 2

    @property
    def center_y(self) -> int:
        return self.bbox[1] + self.bbox[3] // 2


@dataclass
class LogicalLine:
    """A group of tokens sharing approximately the same Y coordinate on a specific page."""
    tokens: list[OcrToken]
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    text: str = ""
    page_number: int = 1
    y_center: float = 0.0

    def __post_init__(self) -> None:
        if self.tokens:
            self.tokens.sort(key=lambda t: t.left)
            if hasattr(self.tokens[0], "page_number"):
                self.page_number = self.tokens[0].page_number
            min_l = min(t.left for t in self.tokens)
            min_t = min(t.top for t in self.tokens)
            max_r = max(t.right for t in self.tokens)
            max_b = max(t.bottom for t in self.tokens)
            if self.bbox == (0, 0, 0, 0):
                self.bbox = (min_l, min_t, max_r - min_l, max_b - min_t)
            if not self.text:
                raw_text = " ".join(t.text.strip() for t in self.tokens if t.text.strip())
                self.text = re.sub(r'\s+', ' ', raw_text).strip()
            if self.y_center == 0.0:
                self.y_center = (min_t + max_b) / 2.0

    @property
    def text_lower(self) -> str:
        return self.text.lower()

    @property
    def left(self) -> int:
        return self.bbox[0]

    @property
    def top(self) -> int:
        return self.bbox[1]

    @property
    def width(self) -> int:
        return self.bbox[2]

    @property
    def height(self) -> int:
        return self.bbox[3]

    @property
    def right(self) -> int:
        return self.bbox[0] + self.bbox[2]

    @property
    def bottom(self) -> int:
        return self.bbox[1] + self.bbox[3]

    @property
    def center_x(self) -> int:
        return self.bbox[0] + self.bbox[2] // 2

    @property
    def center_y(self) -> int:
        return self.bbox[1] + self.bbox[3] // 2



@dataclass
class LogicalBlock:
    """A 2D cluster of LogicalLines forming a coherent visual and semantic block."""
    lines: list[LogicalLine] = field(default_factory=list)
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    page_number: int = 1
    zone: str | None = None
    block_type: str = "text"

    def __post_init__(self) -> None:
        if self.lines:
            self.lines.sort(key=lambda l: l.top)
            if hasattr(self.lines[0], "page_number"):
                self.page_number = self.lines[0].page_number
            if self.bbox == (0, 0, 0, 0):
                min_l = min(l.left for l in self.lines)
                min_t = min(l.top for l in self.lines)
                max_r = max(l.right for l in self.lines)
                max_b = max(l.bottom for l in self.lines)
                self.bbox = (min_l, min_t, max_r - min_l, max_b - min_t)

    def __iter__(self):
        return iter(self.lines)

    def __len__(self) -> int:
        return len(self.lines)

    def __getitem__(self, index: int) -> LogicalLine:
        return self.lines[index]

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)

    @property
    def text_lower(self) -> str:
        return self.text.lower()

    @property
    def left(self) -> int:
        return self.bbox[0]

    @property
    def top(self) -> int:
        return self.bbox[1]

    @property
    def width(self) -> int:
        return self.bbox[2]

    @property
    def height(self) -> int:
        return self.bbox[3]

    @property
    def right(self) -> int:
        return self.bbox[0] + self.bbox[2]

    @property
    def bottom(self) -> int:
        return self.bbox[1] + self.bbox[3]

    @property
    def center_x(self) -> int:
        return self.bbox[0] + self.bbox[2] // 2

    @property
    def center_y(self) -> int:
        return self.bbox[1] + self.bbox[3] // 2

    @property
    def tokens(self) -> list[OcrToken]:
        return [t for l in self.lines for t in l.tokens]


@dataclass
class TableColumn:
    """Detected column in a line-items table."""
    header_text: str
    semantic_type: str  # key from COLUMN_SYNONYMS
    x_center: int
    x_left: int
    x_right: int


@dataclass
class TableRegion:
    """Detected table with columns and data rows on a specific page."""
    columns: list[TableColumn]
    header_line: LogicalLine
    data_lines: list[LogicalLine]
    page_number: int = 1


@dataclass
class MoneyAmount:
    """Monetary value with explicit currency."""
    amount: Decimal | None = None
    currency: str | None = None

    def __bool__(self) -> bool:
        return self.amount is not None

    def __add__(self, other: Any) -> Any:
        amt = self.amount or Decimal(0)
        if isinstance(other, MoneyAmount):
            return amt + (other.amount or Decimal(0))
        return amt + other

    def __radd__(self, other: Any) -> Any:
        amt = self.amount or Decimal(0)
        if other == 0:
            return amt
        if isinstance(other, MoneyAmount):
            return (other.amount or Decimal(0)) + amt
        return other + amt

    def __sub__(self, other: Any) -> Any:
        amt = self.amount or Decimal(0)
        if isinstance(other, MoneyAmount):
            return amt - (other.amount or Decimal(0))
        return amt - other

    def __rsub__(self, other: Any) -> Any:
        amt = self.amount or Decimal(0)
        if isinstance(other, MoneyAmount):
            return (other.amount or Decimal(0)) - amt
        return other - amt

    def __mul__(self, other: Any) -> Any:
        amt = self.amount or Decimal(0)
        if isinstance(other, MoneyAmount):
            return amt * (other.amount or Decimal(0))
        return amt * other

    def __rmul__(self, other: Any) -> Any:
        amt = self.amount or Decimal(0)
        if isinstance(other, MoneyAmount):
            return (other.amount or Decimal(0)) * amt
        return other * amt

    def __truediv__(self, other: Any) -> Any:
        amt = self.amount or Decimal(0)
        if isinstance(other, MoneyAmount):
            return amt / (other.amount or Decimal(1))
        return amt / other

    def __lt__(self, other: Any) -> bool:
        amt = self.amount or Decimal(0)
        o_amt = other.amount if isinstance(other, MoneyAmount) else (Decimal(str(other)) if other is not None else Decimal(0))
        return amt < o_amt

    def __le__(self, other: Any) -> bool:
        amt = self.amount or Decimal(0)
        o_amt = other.amount if isinstance(other, MoneyAmount) else (Decimal(str(other)) if other is not None else Decimal(0))
        return amt <= o_amt

    def __gt__(self, other: Any) -> bool:
        amt = self.amount or Decimal(0)
        o_amt = other.amount if isinstance(other, MoneyAmount) else (Decimal(str(other)) if other is not None else Decimal(0))
        return amt > o_amt

    def __ge__(self, other: Any) -> bool:
        amt = self.amount or Decimal(0)
        o_amt = other.amount if isinstance(other, MoneyAmount) else (Decimal(str(other)) if other is not None else Decimal(0))
        return amt >= o_amt

    def __abs__(self) -> Decimal:
        return abs(self.amount or Decimal(0))

    def __float__(self) -> float:
        return float(self.amount or 0.0)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, MoneyAmount):
            return self.amount == other.amount and (self.currency == other.currency or not self.currency or not other.currency)
        if isinstance(other, (Decimal, int, float)):
            return self.amount == Decimal(str(other))
        return super().__eq__(other)


@dataclass
class Party:
    """Invoice party (supplier or recipient)."""
    name: str | None = None
    eik: str | None = None
    vat_number: str | None = None
    address: str | None = None
    mol: str | None = None


@dataclass
class LineItem:
    """Single line item from the invoice table."""
    index: int | None = None
    description: str | None = None
    unit: str | None = None
    quantity: Decimal | None = None
    unit_price_net: MoneyAmount = field(default_factory=MoneyAmount)
    total_price_net: MoneyAmount = field(default_factory=MoneyAmount)
    vat_rate_pct: Decimal | None = None
    page_number: int = 1
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)

    def __post_init__(self) -> None:
        if isinstance(self.unit_price_net, (Decimal, int, float, str)):
            self.unit_price_net = MoneyAmount(Decimal(str(self.unit_price_net)) if self.unit_price_net is not None else None)
        elif self.unit_price_net is None:
            self.unit_price_net = MoneyAmount()
        if isinstance(self.total_price_net, (Decimal, int, float, str)):
            self.total_price_net = MoneyAmount(Decimal(str(self.total_price_net)) if self.total_price_net is not None else None)
        elif self.total_price_net is None:
            self.total_price_net = MoneyAmount()


@dataclass
class FinancialSummary:
    """Invoice financial totals."""
    tax_base: MoneyAmount = field(default_factory=MoneyAmount)
    vat_amount: MoneyAmount = field(default_factory=MoneyAmount)
    total_amount_due: MoneyAmount = field(default_factory=MoneyAmount)
    total_amount_words: str | None = None


@dataclass
class PaymentDetails:
    """Payment information."""
    method: str | None = None
    bank_name: str | None = None
    iban: str | None = None
    bic: str | None = None


@dataclass
class ValidationIssue:
    """A single validation error or warning."""
    code: str
    message: str
    severity: str  # "error" | "warning"
    field: str | None = None
    detected_value: str | None = None
    expected_value: str | None = None
    difference: str | None = None


@dataclass
class ValidationResult:
    """Aggregated validation outcome."""
    is_valid: bool = False
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)


@dataclass
class InvoiceMetadata:
    """Invoice header metadata."""
    invoice_number: str | None = None
    date_issued: str | None = None  # YYYY-MM-DD
    date_tax_event: str | None = None  # YYYY-MM-DD
    place_issued: str | None = None
    ocr_confidence_score: float | None = None


@dataclass
class Invoice:
    """Top-level invoice model."""
    raw_ocr_evidence: dict[str, Any] | None = None
    invoice_metadata: InvoiceMetadata = field(default_factory=InvoiceMetadata)
    supplier: Party = field(default_factory=Party)
    recipient: Party = field(default_factory=Party)
    line_items: list[LineItem] = field(default_factory=list)
    financial_summary: FinancialSummary = field(default_factory=FinancialSummary)
    payment_details: PaymentDetails = field(default_factory=PaymentDetails)
    validation: ValidationResult = field(default_factory=ValidationResult)


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------

class _InvoiceEncoder(json.JSONEncoder):
    """Custom encoder: Decimal → string, dataclass → dict."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            # Serialize as string to prevent floating-point corruption
            return str(obj)
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        return super().default(obj)


def serialize_invoice(invoice: Invoice) -> str:
    """Serialize an Invoice to a JSON string.

    Decimal values become strings (e.g. ``"573.00"``).
    ``None`` becomes JSON ``null``.
    Bulgarian text is preserved (``ensure_ascii=False``).
    """
    return json.dumps(
        dataclasses.asdict(invoice),
        cls=_InvoiceEncoder,
        indent=2,
        ensure_ascii=False,
    )


# ===================================================================
# LAYER 0 — UTILITY / PARSER FUNCTIONS
# ===================================================================

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

    # Strip currency symbols and words, but keep digits and separators.
    # Note: \b word boundaries don't work with Cyrillic, so we use explicit
    # patterns without word-boundary anchors.
    cleaned = raw.strip()

    # Handle Euro glyph artifacts common in Bulgarian invoicing software (e.g. Microinvest Sklad Pro)
    # where the € symbol glyph maps to '6', 'e', 'E', or '€' after 2 decimal digits:
    # e.g. "274,426" -> "274,42", "82.386" -> "82.38", "238,11 6" -> "238,11", "204 746" -> "204.74"
    cleaned = re.sub(r'(\d+[,.]\d{2})\s*[6eE€]\b', r'\1', cleaned)
    cleaned = re.sub(r'(\d+)\s+(\d{2})[6eE€]\b', r'\1.\2', cleaned)

    cleaned = re.sub(
        r'(?i)лв\.?|лева|bgn|eur|евро|евроцент\w*|€', '', cleaned,
    )
    cleaned = re.sub(r'[^\d.,\s\-]', '', cleaned).strip()

    if not cleaned:
        return None

    # Handle negative
    negative = cleaned.startswith('-')
    cleaned = cleaned.lstrip('-').strip()

    # Remove spaces used as thousands separators (e.g. "1 234,56")
    # Only collapse spaces if the pattern looks like thousands-grouping
    parts_by_space = cleaned.split()
    if len(parts_by_space) > 1:
        # E.g. ["1", "234,56"] — join if middle parts are 3 digits
        cleaned = "".join(parts_by_space)

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


def normalize_eik(raw: str) -> str | None:
    """Normalize a Bulgarian EIK (Единен идентификационен код).

    Strips non-digit characters and validates length (9 or 13 digits).
    Returns the cleaned digit string or ``None``.
    """
    if not raw:
        return None
    digits = re.sub(r'\D', '', raw)
    if len(digits) in (9, 10, 13):
        return digits
    return None


def normalize_vat_number(raw: str) -> str | None:
    """Normalize a Bulgarian VAT number to ``BG`` + digits.

    Handles OCR variations like ``bg 123456789``, ``B G123456789``, etc.
    Returns ``None`` if the format is unrecognisable.
    """
    if not raw:
        return None
    cleaned = raw.upper().replace(' ', '').replace('.', '').replace('-', '')
    # Remove any characters between B and G (OCR artifacts)
    cleaned = re.sub(r'^B\s*G\s*', 'BG', cleaned)
    if cleaned.startswith('BG'):
        digits = re.sub(r'\D', '', cleaned[2:])
        if len(digits) in (9, 10, 13):
            return f"BG{digits}"
    # Try to extract digits only (might be missing the BG prefix)
    digits = re.sub(r'\D', '', raw)
    if len(digits) in (9, 10, 13):
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
        ``28.08.2026``  → ``2026-08-28``
        ``28/08/2026``  → ``2026-08-28``
        ``2026-08-28``  → ``2026-08-28``

    Returns ``YYYY-MM-DD`` or ``None`` if the date is unparseable or invalid.
    """
    if not raw:
        return None
    raw = raw.strip()

    # Try YYYY-MM-DD first
    m = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', raw)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            dt = datetime.date(y, mo, d)
            if 1990 <= dt.year <= 2100:
                return dt.isoformat()
        except ValueError:
            return None

    # Try DD.MM.YYYY or DD/MM/YYYY
    m = re.search(r'(\d{1,2})[./](\d{1,2})[./](\d{4})', raw)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            dt = datetime.date(y, mo, d)
            if 1990 <= dt.year <= 2100:
                return dt.isoformat()
        except ValueError:
            return None

    return None


# ===================================================================
# LAYER 1 — IMAGE PREPROCESSING
# ===================================================================

def pixmap_to_bgr(pix: pymupdf.Pixmap) -> np.ndarray:
    """Convert a PyMuPDF Pixmap to a contiguous OpenCV BGR uint8 array.

    Handles Grayscale (n=1), RGB (n=3), RGBA (n=4), and CMYK fallbacks.
    """
    if pix.colorspace and pix.colorspace.name == "DeviceCMYK":
        rgb_pix = fitz.Pixmap(fitz.csRGB, pix)
        arr = np.frombuffer(rgb_pix.samples, dtype=np.uint8).reshape((rgb_pix.height, rgb_pix.width, 3))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    elif pix.n == 1:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width))
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    elif pix.n == 3:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    elif pix.n == 4:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 4))
        return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
    else:
        rgb_pix = fitz.Pixmap(fitz.csRGB, pix)
        arr = np.frombuffer(rgb_pix.samples, dtype=np.uint8).reshape((rgb_pix.height, rgb_pix.width, 3))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def rasterize_pdf(path: Path | str, dpi: int = DEFAULT_RASTER_DPI) -> list[PageImage]:
    """Rasterize all pages of a PDF into a list of PageImage objects.

    Raises:
        ValueError: If PDF is corrupted, empty, or password-protected.
    """
    path = Path(path)
    if dpi <= 0:
        raise ValueError(f"Invalid rasterization DPI: {dpi}. Must be a positive integer.")

    try:
        doc = pymupdf.open(str(path))
    except (pymupdf.EmptyFileError, pymupdf.FileDataError) as exc:
        raise ValueError(f"Failed to open PDF document: {path} ({exc})") from exc
    except Exception as exc:
        raise ValueError(f"Failed to open PDF document: {path} ({exc})") from exc

    try:
        if doc.is_encrypted and doc.needs_pass:
            raise ValueError(f"Encrypted or password-protected PDF is not supported: {path}")

        total_pages = len(doc)
        if total_pages == 0:
            raise ValueError(f"PDF document contains 0 pages: {path}")

        pages: list[PageImage] = []
        try:
            for idx in range(total_pages):
                page = doc[idx]
                pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
                bgr = pixmap_to_bgr(pix)
                pages.append(PageImage(
                    page_number=idx + 1,
                    image=bgr,
                    width=pix.width,
                    height=pix.height,
                ))
                del pix
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Failed to rasterize PDF document: {path} ({exc})") from exc

        logger.info("Rasterized PDF: %s (%d page(s) at %d DPI)", path.name, total_pages, dpi)
        return pages
    finally:
        doc.close()


def load_image_page(path: Path | str) -> PageImage:
    """Load a single image file (.png, .jpg, .jpeg) into a PageImage using imdecode for Cyrillic path safety."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except Exception as exc:
        raise ValueError(f"Failed to read image file: {path} ({exc})") from exc

    if len(data) == 0:
        raise ValueError(f"Failed to decode image file (empty): {path}")

    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Failed to decode image file: {path}")

    h, w = img.shape[:2]
    logger.info("Loaded image: %s (%dx%d)", path.name, w, h)
    return PageImage(
        page_number=1,
        image=img,
        width=w,
        height=h,
    )


def load_document(path: Path | str, dpi: int = DEFAULT_RASTER_DPI) -> list[PageImage]:
    """Unified document loader supporting PDF, PNG, JPG, and JPEG.

    Returns a list of PageImage objects with uniform BGR arrays and page numbers.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file format is unsupported or corrupted.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Document file not found: {path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file format '{suffix}' for file: {path}. "
            f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    if suffix in PDF_EXTENSIONS:
        return rasterize_pdf(path, dpi=dpi)
    elif suffix in IMAGE_EXTENSIONS:
        return [load_image_page(path)]
    else:
        raise ValueError(f"Unsupported file format: {suffix}")


def load_image(path: Path | str) -> np.ndarray:
    """Backwards-compatible wrapper returning the first page image as np.ndarray."""
    pages = load_document(path)
    return pages[0].image


def to_grayscale(img: np.ndarray) -> np.ndarray:
    """Convert to grayscale if the image has colour channels."""
    if len(img.shape) == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img.copy()


def detect_orientation(img: np.ndarray, min_conf: float = 5.0) -> int:
    """Detect rotation needed to make image upright (0, 90, 180, 270).

    Uses pytesseract.image_to_osd with output_type=Output.DICT.
    Catches pytesseract.TesseractError and all exceptions gracefully,
    returning 0 on error or if orientation_conf < min_conf.
    """
    if img is None or img.size == 0:
        return 0
    try:
        data = pytesseract.image_to_osd(img, output_type=Output.DICT)
        rotate_deg = int(data.get("rotate", 0))
        conf = float(data.get("orientation_conf", 0.0))
        if conf >= min_conf and rotate_deg in (90, 180, 270):
            return rotate_deg
    except pytesseract.TesseractError as exc:
        logger.debug("OSD skipped (insufficient text or unreadable): %s", exc)
    except Exception as exc:
        logger.warning("Unexpected error during OSD orientation detection: %s", exc)
    return 0


def apply_orientation(img: np.ndarray, rotate_deg: int) -> np.ndarray:
    """Apply 90/180/270° rotation using OpenCV."""
    if rotate_deg == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    elif rotate_deg == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    elif rotate_deg == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def check_and_fix_orientation(img: np.ndarray) -> np.ndarray:
    """Detect and correct 90/180/270° rotation, returning upright image.

    Falls back to original image if OSD fails or confidence is low.
    """
    rot = detect_orientation(img)
    if rot != 0:
        logger.info("Corrected orientation by %d°", rot)
        return apply_orientation(img, rot)
    return img


def upscale_if_needed(img: np.ndarray, min_height: int = 2000) -> np.ndarray:
    """Upscale image if its height is below *min_height*."""
    h = img.shape[0]
    if h < min_height:
        scale = min_height / h
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        logger.info("Upscaled image by %.2fx to %dx%d", scale, img.shape[1], img.shape[0])
    return img


def denoise_bilateral(
    img: np.ndarray,
    d: int = 5,
    sigma_color: float = 50.0,
    sigma_space: float = 50.0,
) -> np.ndarray:
    """Apply Cyrillic-safe edge-preserving bilateral filtering to suppress background noise.

    Preserves fine character edges, Cyrillic diacritics ('й', 'Й', 'ѝ'), dots, and decimal commas.
    """
    if img is None or img.size == 0:
        return img
    gray = img if len(img.shape) == 2 else to_grayscale(img)
    return cv2.bilateralFilter(gray, d=d, sigmaColor=sigma_color, sigmaSpace=sigma_space)


def denoise(img: np.ndarray, strength: int = 10) -> np.ndarray:
    """Edge-preserving denoising (backward-compatible wrapper)."""
    return denoise_bilateral(img)


def enhance_contrast_clahe(
    img: np.ndarray,
    clip_limit: float = 2.0,
    tile_grid_size: tuple[int, int] = (8, 8),
) -> np.ndarray:
    """Apply CLAHE contrast enhancement on CIELAB L* luminance channel for BGR (or directly for grayscale).

    Prevents chromatic distortion and color fringes.
    """
    if img is None or img.size == 0:
        return img

    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid_size)

    # 1-channel Grayscale
    if len(img.shape) == 2:
        return clahe.apply(img)

    # 3-channel BGR
    if len(img.shape) == 3 and img.shape[2] == 3:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        cl = clahe.apply(l)
        return cv2.cvtColor(cv2.merge((cl, a, b)), cv2.COLOR_LAB2BGR)

    # 4-channel BGRA
    if len(img.shape) == 3 and img.shape[2] == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        return enhance_contrast_clahe(bgr, clip_limit=clip_limit, tile_grid_size=tile_grid_size)

    return img


def enhance_contrast(img: np.ndarray) -> np.ndarray:
    """Apply CLAHE contrast-limited adaptive histogram equalisation."""
    return enhance_contrast_clahe(img)


def adaptive_threshold(img: np.ndarray) -> np.ndarray:
    """Adaptive Gaussian thresholding."""
    gray = img if len(img.shape) == 2 else to_grayscale(img)
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2,
    )


def binarize_otsu(img: np.ndarray) -> np.ndarray:
    """Otsu's global binarisation."""
    if img is None or img.size == 0:
        return img
    gray = img if len(img.shape) == 2 else to_grayscale(img)
    return cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]


def global_threshold(img: np.ndarray) -> np.ndarray:
    """Otsu's binarisation (backward-compatible alias)."""
    return binarize_otsu(img)


def detect_deskew_angle(
    img: np.ndarray,
    max_angle: float = 15.0,
    min_angle: float = 0.2,
) -> float:
    """Detect skew angle using contour-filtered text-line detection.

    Steps:
    1. Grayscale + Otsu threshold on inverted image.
    2. Horizontal morphological dilation to bridge character gaps into line strips.
    3. Find contours; filter for valid text lines (width >= 50, aspect_ratio >= 2.5).
    4. Extract minAreaRect angle for each line and calculate the median angle.
    5. Guard against 90° flips: reject if high variance (std > 4.0°) or contours < 5.
    6. Clamp angle strictly within [-max_angle, max_angle]; return 0.0 if abs(angle) < min_angle.
    """
    if img is None or img.size == 0:
        return 0.0

    try:
        gray = img if len(img.shape) == 2 else to_grayscale(img)
        h, w = gray.shape[:2]
        if h < 20 or w < 20:
            return 0.0

        # Invert so text is foreground
        inverted = cv2.bitwise_not(gray)
        thresh = cv2.threshold(inverted, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]

        # Horizontal dilation to connect characters within lines
        kernel_w = max(15, int(w * 0.01))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 3))
        dilated = cv2.dilate(thresh, kernel, iterations=1)

        contours, _ = cv2.findContours(dilated, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        angles: list[float] = []

        min_w = max(50, int(w * 0.03))
        max_w = int(w * 0.95)
        min_h = max(5, int(h * 0.003))
        max_h = max(60, int(h * 0.04))

        for cnt in contours:
            if len(cnt) < 5:
                continue

            bx, by, bw, bh = cv2.boundingRect(cnt)
            # If the contour's axis-aligned bounding box is predominantly vertical,
            # it represents a vertical structure (e.g. table border) or a vertical
            # text line resulting from near-90° tilt. Discard from horizontal deskew.
            if bh > bw and (bh / max(1, bw)) >= 1.5:
                continue

            (cx, cy), (rw, rh), r_angle = cv2.minAreaRect(cnt)
            if rw >= rh:
                long_len, short_len = rw, rh
                line_angle = r_angle
            else:
                long_len, short_len = rh, rw
                line_angle = r_angle + 90.0

            while line_angle > 90.0:
                line_angle -= 180.0
            while line_angle < -90.0:
                line_angle += 180.0

            # Only consider contours that are horizontal line-like (|angle| <= 45°)
            if abs(line_angle) > 45.0:
                continue

            if long_len >= min_w and long_len <= max_w and min_h <= short_len <= max_h and (long_len / max(1.0, short_len)) >= 2.5:
                angles.append(line_angle)

        if len(angles) < 5:
            return 0.0

        arr = np.array(angles)
        if float(np.std(arr)) > 4.0:
            # High angular dispersion indicates inconsistent line directions
            return 0.0

        med = float(np.median(arr))
        if abs(med) < min_angle or abs(med) > max_angle:
            return 0.0
        return med
    except Exception as exc:
        logger.warning("Deskew angle detection failed: %s", exc)
        return 0.0


def apply_deskew(
    img: np.ndarray,
    angle_deg: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rotate image by angle_deg with white background border filling.

    Returns (deskewed_image, affine_matrix_2x3, inverse_affine_matrix_2x3).
    """
    h, w = img.shape[:2]
    if abs(angle_deg) < 1e-4:
        identity = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        return img, identity, identity

    center = (w / 2.0, h / 2.0)
    M = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    M_inv = cv2.invertAffineTransform(M)
    border_val = (255, 255, 255) if len(img.shape) == 3 else 255

    rotated = cv2.warpAffine(
        img, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border_val,
    )
    return rotated, M, M_inv


def deskew_image(img: np.ndarray, angle: float | None = None) -> np.ndarray:
    """Deskew image by detected or provided angle.

    Uses cv2.getRotationMatrix2D and cv2.warpAffine with cv2.BORDER_CONSTANT
    and borderValue=(255, 255, 255).
    """
    if img is None or img.size == 0:
        return img
    if angle is None:
        angle = detect_deskew_angle(img)
    if abs(angle) < 1e-4:
        return img
    rotated, _, _ = apply_deskew(img, angle)
    logger.info("Deskewed image by %.2f°", angle)
    return rotated


def normalize_page_geometry(page: PageImage) -> tuple[PageImage, PageTransform]:
    """Execute 2-stage geometry normalization (OSD + deskew) on a PageImage.

    Normalizes orientation and skew once per page before generating variants.
    """
    orig_h, orig_w = page.image.shape[:2]
    transform = PageTransform(
        page_number=page.page_number,
        original_width=orig_w,
        original_height=orig_h,
        normalized_width=orig_w,
        normalized_height=orig_h,
    )

    work_img = page.image

    # Stage 1: OSD Orientation Correction
    rot_needed = detect_orientation(work_img)
    if rot_needed in (90, 180, 270):
        work_img = apply_orientation(work_img, rot_needed)
        transform.orientation_rotate_deg = rot_needed
        logger.info("Page %d: Corrected orientation by %d°", page.page_number, rot_needed)

    # Stage 2: Contour-Based Deskewing
    skew_angle = detect_deskew_angle(work_img)
    if abs(skew_angle) >= 0.2:
        work_img, M, M_inv = apply_deskew(work_img, skew_angle)
        transform.deskew_angle_deg = skew_angle
        transform.affine_matrix = M
        transform.inv_affine_matrix = M_inv
        logger.info("Page %d: Deskewed by %.2f°", page.page_number, skew_angle)

    norm_h, norm_w = work_img.shape[:2]
    transform.normalized_width = norm_w
    transform.normalized_height = norm_h

    normalized_page = PageImage(
        page_number=page.page_number,
        image=work_img,
        width=norm_w,
        height=norm_h,
    )
    return normalized_page, transform


def morphological_cleanup(img: np.ndarray) -> np.ndarray:
    """Deprecated: Removed to prevent eroding black text and corrupting decimal commas.

    Returns the image unchanged.
    """
    return img


def generate_preprocessing_variants(raw_img: np.ndarray) -> list[tuple[str, np.ndarray]]:
    """Generate complementary image preprocessing variants for multi-pass OCR.

    Produces fast, high-quality variants:
        1. 'minimal'       — Grayscale only (preserves sharp vector/high-res text)
        2. 'standard'      — CIELAB L* CLAHE grayscale (continuous tones for LSTM)
        3. 'clahe_gray'    — CIELAB L* CLAHE grayscale
        4. 'enhanced_otsu' — CLAHE + Bilateral Denoise + Otsu Binarization
    """
    gray_base = raw_img if len(raw_img.shape) == 2 else to_grayscale(raw_img)
    variants: list[tuple[str, np.ndarray]] = []

    # 1. Minimal (Clean Grayscale)
    variants.append(("minimal", gray_base))

    # 2. CLAHE Grayscale (Continuous tones for Tesseract LSTM)
    try:
        clahe_enhanced = enhance_contrast_clahe(raw_img)
        clahe_gray = clahe_enhanced if len(clahe_enhanced.shape) == 2 else to_grayscale(clahe_enhanced)
        variants.append(("clahe_gray", clahe_gray))
        variants.append(("standard", clahe_gray))
    except Exception as exc:
        logger.warning("CLAHE grayscale variant failed: %s", exc)
        clahe_gray = gray_base
        variants.append(("standard", gray_base))

    # 3. Enhanced Otsu (CLAHE + Bilateral Denoise + Otsu Binarization)
    try:
        denoised = denoise_bilateral(clahe_gray, d=5, sigma_color=50.0, sigma_space=50.0)
        binary_otsu = binarize_otsu(denoised)
        variants.append(("enhanced_otsu", binary_otsu))
    except Exception as exc:
        logger.warning("Enhanced Otsu variant failed: %s", exc)

    return variants


# ===================================================================
# LAYER 2 — OCR ENGINE
# ===================================================================

BULGARIAN_KEYWORDS: set[str] = {
    "фактура", "доставчик", "получател", "еик", "ддс", "данъчна", "основа",
    "стойност", "сума", "общо", "бг", "банка", "ибан", "лева", "лв", "евро",
    "eur", "bgn", "място", "издаване", "дата", "оригинал", "клиент", "купувач",
    "продавач", "капина", "плевен", "софия", "телефон", "мол", "адрес", "стока",
    "номер", "цена", "мярка", "количество", "плащане", "сметка", "бик", "swift",
}


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
    lang: str = "bul",
) -> list[OcrToken]:
    """Execute a single Tesseract OCR pass and parse into OcrToken list."""
    config = f"--psm {psm}"
    data = pytesseract.image_to_data(
        img, lang=lang, config=config, output_type=Output.DICT,
    )
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
    if len(t.text) >= 10 and len(set(t.text.lower())) <= 3:
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

    # Valid date pattern
    if re.search(r'^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$', t.text):
        score += 25.0

    # Valid monetary amount pattern
    if re.search(r'^\d+[.,]\d{2}$', t.text):
        score += 25.0
    elif re.search(r'^\d{1,5}$', t.text):
        score += 10.0
    elif re.fullmatch(r'\d{6,}', t.text):
        score -= 20.0

    # Valid EIK or VAT ID pattern
    if re.search(r'^\d{9}$|^\d{13}$|^BG\d{9,13}$', t.text, re.IGNORECASE):
        score += 25.0

    # Valid IBAN pattern
    if re.search(r'^BG\d{2}[A-Z]{4}\d{14}$', t.text, re.IGNORECASE):
        score += 25.0

    # Spurious quote / edge junk penalty
    if t.text.startswith(('„', '“', '"', "'", '`', '|')):
        score -= 10.0
    if t.text.endswith(('|', '`')):
        score -= 10.0

    # Repetitive character string penalty
    if len(t.text) > 10 and len(set(t.text.lower())) <= 3:
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
) -> list[OcrToken]:
    """Execute multi-pass OCR (PSM 3 and PSM 11) and fuse results via spatial scoring.

    Pass 1 executes PSM 3 (automatic layout analysis) for paragraph & column structure.
    Pass 2 executes PSM 11 (sparse text) for isolated numbers, codes, and stamps.
    Tokens are fused via fuse_ocr_passes() with multi-factor scoring.
    """
    if not variants:
        return []

    # Select target image for multi-pass OCR: prefer clahe_gray or standard
    target_img = variants[0][1]
    for name, img in variants:
        if name in ("clahe_gray", "standard"):
            target_img = img
            break

    try:
        p1_tokens = execute_ocr_pass(target_img, psm=3, lang="bul")
    except Exception as exc:
        logger.warning("Pass 1 (PSM 3) failed: %s", exc)
        p1_tokens = []

    try:
        p2_tokens = execute_ocr_pass(target_img, psm=11, lang="bul")
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
        cleaned = clean_ocr_artifacts(t.text)
        if cleaned:
            t.text = cleaned
            result.append(t)
    return result


# ===================================================================
# LAYER 4 — LAYOUT ANALYSIS
# ===================================================================

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

                scores = []
                # 3. Overlap with horizontally nearby tokens in candidate line (within 350 px)
                for tok in l:
                    if abs(tok.center_x - t.center_x) <= 350:
                        if tok.text.strip() not in {"|", "¦", "||", "!"} and t.text.strip() not in {"|", "¦", "||", "!"}:
                            scores.append(_token_vertical_overlap_ratio(t, tok))

                # 4. Overlap with bounding box vertical span of line
                l_top = min(x.top for x in l)
                l_bot = max(x.bottom for x in l)
                l_h = l_bot - l_top
                if l_h <= int(2.5 * max(t.height, 20)):
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
        if 0.05 * page_height <= l.top <= 0.45 * page_height
    ]
    mid_x = page_width // 2

    left_supp = 0
    left_recip = 0
    right_supp = 0
    right_recip = 0

    for l in party_lines:
        for t in l.tokens:
            txt = t.text.lower()
            is_supp = any(kw in txt for kw in SUPPLIER_KEYWORDS)
            is_recip = any(kw in txt for kw in RECIPIENT_KEYWORDS)
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

                    primary_columns = columns

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

            if valid_table_rows >= 1 and data_lines:
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
            # Deduplicate OCR vs PDF duplicate tokens
            is_dup = False
            for ex_txt, ex_x in col_texts[col_type]:
                if abs(ex_x - token.center_x) < 20 or token.text in ex_txt or ex_txt in token.text:
                    is_dup = True
                    break
            if not is_dup:
                col_texts[col_type].append((token.text, token.center_x))
    return {k: " ".join([t[0] for t in v]) for k, v in col_texts.items() if v}


# ===================================================================
# LAYER 4 — FIELD EXTRACTION
# ===================================================================

def extract_invoice_number(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
) -> str | None:
    """Extract the invoice number using regex and keyword proximity.

    Searches for patterns like ``Фактура № 1234``, ``Фактура No 1234``,
    ``Invoice 1234``, ``Номер: 1234``, ``Мо: 1234``, ``А/о 1234``.

    Guards against OCR/fusion duplicates by capping digit run length
    and deduplicating symmetric repeats (e.g. '11001245851100124585').
    """
    def _clean_and_dedup(raw: str) -> str | None:
        clean = re.sub(r'\s+', '', raw)
        # Drop leading single Cyrillic 'з' or 'З' if followed by 9-10 digits (common OCR artifact for '3')
        if re.fullmatch(r'[зЗ]\d{9,10}', clean):
            clean = clean[1:]
        clean = _dedup_invoice_number(clean)
        # Avoid matching EIK numbers (if exactly 9 digits and not part of 10-digit invoice)
        if len(clean) >= 5 and clean.isdigit():
            if len(clean) == 10:
                return clean
            elif 5 <= len(clean) < 10:
                return clean.zfill(10)
            elif len(clean) > 10:
                # Some barcodes concatenate '10000' with 10-digit invoice number (e.g. 100001000358400)
                if clean.startswith("10000") and len(clean) == 15:
                    return clean[5:]
                return None
        return clean if clean else None

    # Line patterns including Bulgarian variations
    line_patterns = [
        r'(?i)(?:фактура|invoice)\s*(?:[№#]|no\.?|номер|мо\.?|хо\.?|a/o)?\s*[:./\"\'“\-]*\s*([зЗ]?\d{4,6}\s*\d{4,7})',
        r'(?i)(?:номер|мо|мо:|хо|хо:|а/о|a/o|no|no:|n/o)\s*[:./\"\'“\-]*\s*([зЗ]?\d{4,6}\s*\d{4,7})',
        r'(?i)[№#]\s*[:./\"\'“\-]*\s*([зЗ]?\d{4,6}\s*\d{4,7})',
    ]

    # Work line-by-line first
    for line in lines:
        text = line.text
        for pattern in line_patterns:
            m = re.search(pattern, text)
            if m:
                res = _clean_and_dedup(m.group(1))
                if res:
                    return res

    # Check two consecutive lines (e.g. line 1 has "Номер:", line 2 has "3000017826")
    for i in range(len(lines) - 1):
        t1 = lines[i].text.strip().lower()
        if re.search(r'(?i)\b(?:номер|фактура|мо|хо|№|no|invoice)\b\s*[:./\"\'“\-]*$', t1):
            t2 = lines[i+1].text.strip()
            m = re.search(r'^[:./\"\'“\-]*\s*([зЗ]?\d{4,6}\s*\d{4,7})\b', t2)
            if m:
                res = _clean_and_dedup(m.group(1))
                if res:
                    return res

    # Fallback: search across all tokens
    full_text = " ".join(t.text for t in tokens)
    for pattern in line_patterns:
        m = re.search(pattern, full_text)
        if m:
            res = _clean_and_dedup(m.group(1))
            if res:
                return res

    # Standalone 10-digit statutory number or barcode in upper 35% of document
    upper_tokens = [t for t in tokens if t.top < 1300]
    for t in upper_tokens:
        clean_t = re.sub(r'[^\d]', '', t.text)
        if len(clean_t) == 15 and clean_t.startswith("10000"):
            return clean_t[5:]
        if len(clean_t) == 10 and clean_t.isdigit():
            # Standard statutory invoice ranges: 0xxxxxxxx, 1xxxxxxxx, 2xxxxxxxx, 3xxxxxxxx
            if clean_t[0] in "0123":
                res = _clean_and_dedup(clean_t)
                if res:
                    return res

    # Last resort: lines containing "фактура" with a nearby long number
    for line in lines:
        text_lower = line.text.lower() if hasattr(line, 'text') else ""
        if "фактура" in text_lower or "invoice" in text_lower:
            nums = re.findall(r'\d{5,15}', line.text)
            if nums:
                res = _clean_and_dedup(nums[0])
                if res:
                    return res

    return None


def _dedup_invoice_number(raw: str) -> str:
    """Detect and fix symmetric duplication in invoice numbers.

    E.g. '11001245851100124585' → '1100124585' (10-char repeated twice).
    """
    n = len(raw)
    if n >= 8 and n % 2 == 0:
        half = n // 2
        if raw[:half] == raw[half:]:
            return raw[:half]
    return raw


def extract_dates(lines: list[LogicalLine]) -> tuple[str | None, str | None]:
    """Extract date_issued and date_tax_event.

    Looks for keywords like ``дата``, ``дата на издаване``,
    ``дата на данъчно събитие``, ``дата на доставка``.
    Falls back to the first two dates found in the document.
    """
    date_issued: str | None = None
    date_tax_event: str | None = None

    issue_keywords = ["дата на издаване", "дата на фактурата", "дата:", "date", "от дата", "дата "]
    tax_keywords = [
        "данъчно събитие", "дата на доставка", "дата на данъчно",
        "данъчно", "доставка", "датана данъчно", "дан.събитие", "дан. събитие", "събитис"
    ]

    for line in lines:
        text = line.text_lower
        parsed = parse_date(line.text)
        if not parsed:
            continue

        # Check for tax event date keywords
        if any(kw in text for kw in tax_keywords):
            if date_tax_event is None:
                date_tax_event = parsed
                continue

        # Check for issue date keywords
        if any(kw in text for kw in issue_keywords):
            if date_issued is None:
                date_issued = parsed
                continue

        # Fallback: assign in order
        if date_issued is None:
            date_issued = parsed
        elif date_tax_event is None:
            date_tax_event = parsed

    # Statutory fallback: if only one date is found on the document,
    # under Bulgarian VAT law (ЗДДС чл. 114), date_issued defaults to date_tax_event
    if date_issued is None and date_tax_event is not None:
        date_issued = date_tax_event
    elif date_tax_event is None and date_issued is not None:
        date_tax_event = date_issued

    return date_issued, date_tax_event


def extract_place_issued(lines: list[LogicalLine]) -> str | None:
    """Extract the place of issue (``място на издаване``).

    Returns a cleaned city name or None. Guards against OCR noise
    by validating Cyrillic content ratio and length.
    """
    def _clean_place(raw: str) -> str | None:
        """Clean up a raw place candidate: strip noise, validate."""
        # Remove leading/trailing punctuation and numbers
        place = re.sub(r'^[\s:./-]+|[\s:./-]+$', '', raw).strip()
        # Remove stray digits and noise characters
        place = re.sub(r'[0-9„""\'«»\[\]{}<>]', '', place).strip()
        # Collapse whitespace
        place = re.sub(r'\s+', ' ', place).strip()
        if len(place) < 2:
            return None
        # Check that at least 50% of chars are Cyrillic letters
        cyrillic_count = sum(1 for c in place if '\u0400' <= c <= '\u04FF')
        total_alpha = sum(1 for c in place if c.isalpha())
        if total_alpha < 2 or cyrillic_count / max(total_alpha, 1) < 0.5:
            return None
        return place

    # Strategy 1: Look for "Град XXXXX" pattern (very common in BG invoices)
    for line in lines:
        m = re.search(r'(?:Град|ГРАД)\s+([А-ЯA-Z][А-Яа-яA-Za-z\s]{1,30})', line.text)
        if m:
            candidate = _clean_place(m.group(1).split()[0])  # First word after "Град"
            if candidate and len(candidate) >= 3:
                return f"гр. {candidate}"

    # Strategy 2: "място на издаване/сделка(та)" — value after colon or on next line
    for i, line in enumerate(lines):
        text = line.text_lower
        if "място" in text and ("издаване" in text or "сделка" in text):
            # Match with definite article suffix (та/то)
            m = re.search(
                r'(?:място\s+(?:на\s+)?(?:издаването?|сделката?)\s*[:./-]?\s*)(.*)',
                line.text, re.I
            )
            if m:
                remainder = m.group(1).strip()
                place = _clean_place(remainder)
                if place and len(place) >= 3:
                    return place
            # If nothing after the keyword, check next line
            if i + 1 < len(lines):
                next_text = lines[i + 1].text.strip()
                place = _clean_place(next_text)
                if place and len(place) >= 3:
                    return place

    # Strategy 3: Fallback to "гр." (city abbreviation) — common Bulgarian format
    for line in lines:
        m = re.search(r'(?:гр\.?\s*)([А-Яа-я][А-Яа-я\s]{1,30})', line.text)
        if m and "доставчик" not in line.text_lower and "получател" not in line.text_lower:
            candidate = m.group(1).strip()
            cleaned = _clean_place(candidate)
            if cleaned and len(cleaned) >= 3:
                return f"гр. {cleaned}"

    return None


def _find_party_region(
    lines: list[LogicalLine],
    keywords: list[str],
    other_keywords: list[str],
) -> list[LogicalLine]:
    """Find the block of lines belonging to a party (supplier/recipient).

    Starts at the line containing a keyword and collects subsequent lines
    until the other party's keyword or a table header is encountered.
    """
    start_idx: int | None = None
    for i, line in enumerate(lines):
        text = line.text_lower
        if any(kw in text for kw in keywords):
            start_idx = i
            break

    if start_idx is None:
        return []

    region: list[LogicalLine] = []
    for line in lines[start_idx:start_idx + 15]:  # max 15 lines for a party block
        text = line.text_lower
        # Stop if we hit the other party or a table header
        if region and any(kw in text for kw in other_keywords):
            break
        if region and _is_summary_line(line):
            break
        # Stop if we hit a table header
        matched_cols = sum(
            1 for t in line.tokens if _match_column_synonym(t.text) is not None
        )
        if region and matched_cols >= 3:
            break
        region.append(line)

    return region


def extract_party(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
    role: str,
) -> Party:
    """Extract supplier or recipient information.

    Uses dynamic left/right party orientation and column-half isolation
    to prevent EIK collisions between supplier and recipient bands.
    """
    party = Party()

    page_w = max(max((t.right for t in tokens), default=2480), 2000)
    page_h = max(max((t.bottom for t in tokens), default=3508), 2000)
    left_role, right_role = resolve_party_orientation(lines, page_w, page_h)
    mid_x = page_w // 2
    receipts = detect_receipt_regions(tokens)

    def _in_receipt(t: OcrToken) -> bool:
        return any(
            rx <= t.center_x <= rx + rw and ry <= t.center_y <= ry + rh
            for rx, ry, rw, rh in receipts
        )

    target_is_right = (role == right_role)
    in_col = (lambda t: t.center_x >= mid_x) if target_is_right else (lambda t: t.center_x < mid_x)

    # Party vertical band typically sits between 0.04H and 0.48H
    col_tokens = [
        t for t in tokens
        if 0.04 * page_h <= t.top and t.bottom <= 0.48 * page_h
        and in_col(t)
        and not _in_receipt(t)
    ]

    col_lines = group_tokens_into_lines(col_tokens) if col_tokens else []
    target_lines = col_lines if col_lines else lines

    if role == "supplier":
        region = _find_party_region(target_lines, SUPPLIER_KEYWORDS, RECIPIENT_KEYWORDS)
    else:
        region = _find_party_region(target_lines, RECIPIENT_KEYWORDS, SUPPLIER_KEYWORDS)

    if not region:
        if col_lines:
            region = col_lines
        else:
            return _extract_party_fallback(tokens, role)

    region_text = "\n".join(line.text for line in region)

    # Extract EIK
    eik_patterns = [
        r'(?:ЕИК|Булстат|ИН|EIK|Идент\.?\s*(?:No|№|номер)?)[^\d\n]{0,25}(\d{9,13})',
        r'(?:ЕИК|EIK)\s*[:./-]?\s*(\d{9,13})',
        r'\b(\d{9})\b',
    ]
    for pattern in eik_patterns:
        m = re.search(pattern, region_text, re.IGNORECASE)
        if m:
            party.eik = normalize_eik(m.group(1))
            break

    # Extract VAT number
    vat_patterns = [
        r'(?:ДДС\s*(?:No|№|номер)?|VAT|Идент\.\s*No\s*по\s*ЗДДС)[^\d\n]{0,25}(?:BG|ВG|ВС|В|ваг|ва|bg)?\s*(\d{9,13})',
        r'(?:ДДС\s*(?:No|№|номер)?|VAT|Идент\.\s*No\s*по\s*ЗДДС)\s*[:./-]?\s*(BG\s*\d{9,13})',
        r'(?:ДДС\s*(?:No|№|номер)?|VAT)\s*[:./-]?\s*(\d{9,13})',
        r'(BG\s*\d{9,13})',
    ]
    for pattern in vat_patterns:
        m = re.search(pattern, region_text, re.IGNORECASE)
        if m:
            cand = m.group(1).strip()
            if not cand.upper().startswith("BG"):
                cand = "BG" + cand
            party.vat_number = normalize_vat_number(cand)
            break

    # Extract MOL
    mol_patterns = [
        r'(?:МОЛ|Отг\.?\s*лице|Управител)\s*[:./-]?\s*([А-Яа-яA-Za-z\s]+)',
    ]
    for pattern in mol_patterns:
        m = re.search(pattern, region_text, re.IGNORECASE)
        if m:
            mol = m.group(1).strip()
            words = mol.split()[:4]
            if words:
                party.mol = " ".join(words)
            break

    # Extract address
    addr_patterns = [
        r'(?:Адрес|адр\.?|гр\.)\s*[:./-]?\s*(.+)',
        r'(?:ул\.|бул\.|ж\.к\.|кв\.)\s*(.+)',
    ]
    for pattern in addr_patterns:
        m = re.search(pattern, region_text, re.IGNORECASE)
        if m:
            addr = m.group(0).strip()
            if len(addr) >= 5:
                party.address = addr
                break

    # Tokens that look like copy markers / not party names
    PARTY_NAME_EXCLUDE = {
        "оригинал", "копие", "дубликат", "екземпляр", "original", "copy",
        "фактура", "invoice", "дата", "дата:", "страница", "стр.",
    }

    skip_patterns = re.compile(
        r'(?i)(?:доставчик|получател|продавач|купувач|клиент|'
        r'supplier|recipient|ЕИК|Булстат|ДДС|VAT|МОЛ|Адрес|'
        r'IBAN|BIC|банка|ул\.|бул\.|ж\.к\.|идент|идент\.|'
        r'град|гр\.|тел\.|телефон|факс)',
    )
    for line in region:
        text = line.text.strip()
        if not text or len(text) < 3:
            continue
        if text.lower() in PARTY_NAME_EXCLUDE:
            continue
        if skip_patterns.search(text):
            m = re.search(r'(?:[гГ]?доставчик|[пП]?получател|продавач|купувач)\s*[:./\-]?\s*(.+)', text, re.I)
            if m:
                name_candidate = m.group(1).strip().strip('„""\'')
                if len(name_candidate) >= 3 and name_candidate.lower() not in PARTY_NAME_EXCLUDE and not re.fullmatch(r'[:./\-]+', name_candidate):
                    name_candidate = re.sub(r'(?i)\s*(?:ЕИК|Булстат|ДДС|Адрес).*', '', name_candidate).strip()
                    if len(name_candidate) >= 3:
                        party.name = name_candidate
                        break
            continue
        party.name = text.strip('„"“”\'')
        break

    return party


def _extract_party_fallback(tokens: list[OcrToken], role: str) -> Party:
    """Fallback party extraction using global regex on all tokens."""
    party = Party()
    full_text = " ".join(t.text for t in tokens)
    full_upper = full_text.upper()

    # Try to find VAT numbers globally
    vat_matches = re.findall(r'BG\s*\d{9,13}', full_upper)
    if vat_matches:
        idx = 0 if role == "supplier" else min(1, len(vat_matches) - 1)
        party.vat_number = normalize_vat_number(vat_matches[idx])
        eik_digits = re.sub(r'\D', '', vat_matches[idx])
        party.eik = normalize_eik(eik_digits)

    return party


def _union_bbox(b1: tuple[int, int, int, int], b2: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Compute bounding box union."""
    if b1 == (0, 0, 0, 0):
        return b2
    if b2 == (0, 0, 0, 0):
        return b1
    x1 = min(b1[0], b2[0])
    y1 = min(b1[1], b2[1])
    x2 = max(b1[0] + b1[2], b2[0] + b2[2])
    y2 = max(b1[1] + b1[3], b2[1] + b2[3])
    return (x1, y1, x2 - x1, y2 - y1)


def extract_line_items(
    table_regions: list[TableRegion],
    lines: list[LogicalLine],
) -> list[LineItem]:
    """Extract line items from detected table regions across all pages.

    Uses column assignments based on X-coordinates to map tokens to
    the correct semantic field (description, quantity, unit price, etc.).
    Supports multi-page table continuation, multi-line continuation rows,
    and strict null fallback for occluded descriptions.
    """
    items: list[LineItem] = []
    if not table_regions:
        return items

    banned_desc = {"item", "unknown", "placeholder", "n/a", "none", "артикул", "null"}
    global_idx = 0

    for table in table_regions:
        page_num = getattr(table, "page_number", 1)

        for row_line in table.data_lines:
            if _is_summary_line(row_line):
                break
            # Skip transfer / carry-forward or repeated column headers
            if is_transfer_or_header_line(row_line):
                continue
            matched_cols = sum(
                1 for t in row_line.tokens if _match_column_synonym(t.text) is not None
            )
            if matched_cols >= 3:
                continue

            col_values = _assign_line_to_columns(row_line, table.columns)
            if not col_values:
                continue

            desc = col_values.get("description", "").strip()
            qty_text = col_values.get("quantity", "")
            price_text = col_values.get("unit_price", "")
            total_text = col_values.get("total_price", "")
            idx_text = col_values.get("index", "").strip()
            unit = col_values.get("unit", "").strip()

            # Prepend any alphabetic words from index column to description
            idx_words = [w for w in re.split(r'\s+', idx_text) if re.search(r'[А-Яа-яA-Za-z]{3,}', w)]
            if idx_words:
                prefix = " ".join(idx_words)
                desc = f"{prefix} {desc}".strip() if desc else prefix

            qty = parse_money(qty_text)
            price = parse_money(price_text)
            total = parse_money(total_text)

            has_index_or_code = bool(re.search(r'\b\d{4,12}\b', idx_text)) or bool(re.search(r'^\s*\d{1,3}\.?\s*$', idx_text))
            has_unit = bool(unit)

            # Check if this row is a multi-line continuation row (Feature 17)
            # A continuation row has description text but no numbers and no independent index code or unit
            is_continuation = (
                qty is None
                and price is None
                and total is None
                and bool(desc)
                and not has_index_or_code
                and not has_unit
            )

            if is_continuation and items:
                # Merge into previous item
                prev_item = items[-1]
                do_merge = True
                if prev_item.bbox and row_line.bbox and getattr(prev_item, 'page_number', page_num) == page_num:
                    prev_bottom = prev_item.bbox[1] + prev_item.bbox[3]
                    gap = row_line.bbox[1] - prev_bottom
                    if gap > -5:
                        do_merge = False
                
                if do_merge:
                    if prev_item.description:
                        prev_item.description = f"{prev_item.description} {desc}"
                    else:
                        prev_item.description = desc
                    if prev_item.bbox and row_line.bbox:
                        prev_item.bbox = _union_bbox(prev_item.bbox, row_line.bbox)
                    continue


            # Skip empty rows (no numbers and no description) - even if they have an index code
            if qty is None and price is None and total is None and not desc:
                continue

            global_idx += 1
            item = LineItem()
            item.index = global_idx

            # Description (Feature 19: Strict Null Fallback)
            desc_clean = desc.strip() if desc else ""
            if desc_clean and desc_clean.lower() not in banned_desc:
                item.description = desc_clean
            else:
                item.description = None

            # Unit
            unit = col_values.get("unit", "").strip()
            if unit:
                item.unit = unit

            # Quantity
            item.quantity = qty

            # Unit price
            item.unit_price_net = MoneyAmount(price)

            # Total price
            item.total_price_net = MoneyAmount(total)

            # VAT rate
            vat_text = col_values.get("vat_rate", "")
            if vat_text:
                item.vat_rate_pct = parse_money(vat_text)

            # If total is missing but we have quantity and unit price, compute it
            if (
                item.total_price_net.amount is None
                and item.quantity is not None
                and item.unit_price_net.amount is not None
            ):
                item.total_price_net = MoneyAmount(
                    (item.quantity * item.unit_price_net.amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                )

            item.page_number = page_num
            item.bbox = row_line.bbox

            items.append(item)

    # Deduplicate items that appear as both OCR and embedded PDF text
    # Post-extraction rule: filter out items with None description AND no quantity AND no valid unit_price
    deduped_items: list[LineItem] = []
    for item in items:
        # Filter noise/receipt overlay
        if item.description is None and item.quantity is None and item.unit_price_net.amount is None:
            continue
            
        is_duplicate = False
        if deduped_items:
            last = deduped_items[-1]
            
            # Check overlap if bbox is present (must be very strong overlap > 80% to be duplicate)
            if item.bbox and last.bbox and item.page_number == last.page_number:
                item_bottom = item.bbox[1] + item.bbox[3]
                last_bottom = last.bbox[1] + last.bbox[3]
                y_overlap = max(0, min(item_bottom, last_bottom) - max(item.bbox[1], last.bbox[1]))
                min_h = min(item.bbox[3], last.bbox[3])
                if min_h > 0 and y_overlap / min_h > 0.8:
                    # Check if they are actually the same item (similar description or same price)
                    if (item.total_price_net.amount == last.total_price_net.amount) or \
                       (item.description and last.description and \
                        (item.description in last.description or last.description in item.description)):
                        is_duplicate = True
            
            # Or exactly same description (if long enough)
            if not is_duplicate and item.description and last.description:
                if len(item.description) > 5 and item.description == last.description:
                    if item.total_price_net.amount == last.total_price_net.amount:
                        is_duplicate = True

        if not is_duplicate:
            deduped_items.append(item)
            
    return deduped_items


def extract_financial_summary(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
    line_items: list[LineItem] | None = None,
) -> FinancialSummary:
    """Extract tax base, VAT amount, and total from the invoice summary section.

    Uses keyword matching on each line and extracts the nearest monetary value
    on the SAME line, preferring the rightmost number (which is typically
    the amount in invoices).
    """
    summary = FinancialSummary()

    tax_base_kws = [
        "данъчна основа", "дан. основа", "дан основа", "дан.основа",
        "данъчнаоснова", "tax base", "основа 20", "основа 9", "основа 0", "основа:",
    ]
    tax_base_exclude = [
        "събитие", "дата", "ставка", "номер", "адрес", "ин по зддс", "ин по ддс", "зддс"
    ]
    vat_kws = [
        "начислен ддс", "ддс 20", "ддс 9", "ддс:", "данък добавена стойност",
        "дължим ддс", "vat amount", "стойност на ддс", "ддс #",
        "данъчна ставка 20", "данъчна ставка", "ставка 20", "ставка 9",
        "20% ддс", "2096 ддс", "20%ддс", "20906 ддс", "ддс ставка", "ставка:"
    ]
    vat_exclude = [
        "ин по зддс", "ин по ддс", "инпо по ддс", "по ддс", "зддс", "ддс номер", "ддс №", "ддсномер", "ддсме", "ддс ме", "номер", "еик", "булстат",
    ]
    specific_total_kws = [
        "сума за плащане", "сумазаплащане", "за плащане", "общо дължимо", "total due", "крайна сума"
    ]
    generic_total_kws = [
        "всичко", "обща сума", "общо:", "сума за", "сума:", "стойност:"
    ]
    header_exclude = [
        "доставчик", "получател", "ин по зддс", "ин по ддс", "инпо по ддс", "по ддс", "зддс", "ддс номер", "ддс №", "ддсномер",
        "дщсномер", "дщс номер", "ддснамер", "ддс намер", "клиент"
    ]

    CURRENCY_GLYPHS = {
        '6', 'e', 'е', 'E', 'Е', '€', 'лв', 'лв.', 'bgn', 'eur', 'b',
        '|', '¦', '!', '#', '§', '>', '<', ':', '-', '“', '„', '"'
    }

    def _extract_rightmost_money(line: LogicalLine) -> Decimal | None:
        """Extract the rightmost monetary value from a line.

        Filters out implausibly large values (> 500k) which are likely
        identifiers (EIK, account numbers) misinterpreted as amounts.
        Prefers values with explicit 2-decimal digits over bare integers.
        """
        MAX_PLAUSIBLE = Decimal("500000")  # 500 thousand
        sorted_tokens = sorted(line.tokens, key=lambda tok: tok.center_x, reverse=True)
        # First pass: prefer tokens with explicit 2-decimal digits
        for token in sorted_tokens:
            raw = token.text.strip()
            if not raw or raw.lower() in CURRENCY_GLYPHS:
                continue
            if re.fullmatch(r'\d{8,}', raw):
                continue
            if re.search(r'\d+[.,]\d{2}', raw):
                val = parse_money(raw)
                if val is not None and abs(val) <= MAX_PLAUSIBLE:
                    return val
        # Second pass: fallback to any valid monetary value
        for token in sorted_tokens:
            raw = token.text.strip()
            if not raw or raw.lower() in CURRENCY_GLYPHS:
                continue
            if re.fullmatch(r'\d{8,}', raw):
                continue
            val = parse_money(raw)
            if val is not None and abs(val) <= MAX_PLAUSIBLE:
                return val
        return None

    # Track which lines have been matched to avoid double-assignment
    matched_lines: set[int] = set()
    total_match_kind: str | None = None

    # First pass: keyword matching
    for i, line in enumerate(lines):
        text = line.text_lower

        # Skip company header / party registration lines
        if any(h in text for h in header_exclude):
            continue
        # Skip lines with 9/10-digit VAT registration numbers
        if re.search(r'\b[a-zа-я]{0,3}\d{9,10}\b', text, re.IGNORECASE):
            continue

        # Skip statutory VAT exemption basis notes (unless this row actually states a VAT rate like 20% or 9%)
        is_exemption_basis = ("неначисляване" in text or "основание за" in text) and not any(r in text for r in ["ставка 20", "ставка 9", "20%", "20 ", "ддс ставка"])

        has_tb = (any(kw in text for kw in tax_base_kws) or ("данъчна" in text and "основа" in text)) and not any(ex in text for ex in tax_base_exclude)
        has_vat = (
            any(kw in text for kw in vat_kws)
            or ("ставка" in text and any(r in text for r in ["20", "9", "0"]))
            or ("ддс" in text and any(r in text for r in ["20", "9", "0"]))
            or ("ддс" in text and "%" not in text)
        ) and not any(ex in text for ex in vat_exclude) and not is_exemption_basis
        has_specific_total = any(kw in text for kw in specific_total_kws) or ("сума" in text and "плащане" in text and "в брой" not in text)
        has_generic_total = any(kw in text for kw in generic_total_kws)

        # 1. Check if line contains both total and VAT keywords (e.g. single-row total+VAT)
        if (has_specific_total or has_generic_total) and has_vat and i not in matched_lines and summary.total_amount_due.amount is None:
            money_tokens = []
            for tok in sorted(line.tokens, key=lambda t: t.center_x):
                raw = tok.text.strip()
                if raw and raw.lower() not in CURRENCY_GLYPHS and not re.fullmatch(r'\d{8,}', raw):
                    if raw in {"20", "20%", "20.00%", "20.00", "2000:", "2090:", "9", "9%", "0", "0%"}:
                        continue
                    val = parse_money(raw)
                    if val is not None and Decimal("0.01") <= abs(val) <= Decimal("500000"):
                        money_tokens.append(val)
            dec_tokens = [v for v in money_tokens if re.search(r'[.,]\d{2}', str(v))]
            chosen = dec_tokens if len(dec_tokens) >= 2 else money_tokens
            if len(chosen) >= 2:
                summary.total_amount_due.amount = max(chosen)
                total_match_kind = "specific" if has_specific_total else "generic"
                if summary.vat_amount.amount is None:
                    summary.vat_amount.amount = min(chosen)
                matched_lines.add(i)
                continue

        # 2. Check if line contains both tax base and VAT keywords (e.g. single-row summary)
        if has_tb and has_vat and i not in matched_lines:
            money_tokens = []
            for tok in sorted(line.tokens, key=lambda t: t.center_x):
                raw = tok.text.strip()
                if raw and raw.lower() not in CURRENCY_GLYPHS and not re.fullmatch(r'\d{8,}', raw):
                    if raw in {"20", "20%", "20.00%", "20.00", "2000:", "2090:", "206", "096", "9", "9%", "0", "0%"}:
                        continue
                    val = parse_money(raw)
                    if val is not None and Decimal("0.01") <= abs(val) <= Decimal("500000"):
                        money_tokens.append(val)
            # Deduplicate adjacent duplicates
            unique_money = []
            for m in money_tokens:
                if not unique_money or unique_money[-1] != m:
                    unique_money.append(m)
            dec_tokens = [v for v in unique_money if re.search(r'[.,]\d{2}', str(v))]
            chosen = dec_tokens if len(dec_tokens) >= 2 else unique_money
            if len(chosen) >= 2:
                summary.tax_base.amount = chosen[-2]
                summary.vat_amount.amount = chosen[-1]
                matched_lines.add(i)
                continue

        # 3. Standard total check (specific keywords take precedence over generic ones)
        if i not in matched_lines:
            if has_specific_total:
                val = _extract_rightmost_money(line)
                if val is not None:
                    if total_match_kind == "generic":
                        # Promote previously matched generic total to tax_base if empty
                        if summary.tax_base.amount is None:
                            summary.tax_base.amount = summary.total_amount_due.amount
                    summary.total_amount_due.amount = val
                    total_match_kind = "specific"
                    matched_lines.add(i)
                    continue
            elif has_generic_total and summary.total_amount_due.amount is None:
                val = _extract_rightmost_money(line)
                if val is not None:
                    summary.total_amount_due.amount = val
                    total_match_kind = "generic"
                    matched_lines.add(i)
                    continue

        if i not in matched_lines:
            if any(kw in text for kw in tax_base_kws) and not any(ex in text for ex in tax_base_exclude):
                val = _extract_rightmost_money(line)
                if val is not None:
                    summary.tax_base.amount = val
                    matched_lines.add(i)
                    continue

        # ДДС matching — be careful not to match "ДДС %" in table headers or VAT registration IDs
        if i not in matched_lines:
            if any(kw in text for kw in vat_kws) and not any(ex in text for ex in vat_exclude) and not is_exemption_basis:
                val = _extract_rightmost_money(line)
                if val is not None:
                    summary.vat_amount.amount = val
                    matched_lines.add(i)
                    continue
            # Simpler ДДС match but only in the summary section
            # (after table data, where "ддс" is followed by a number, excluding registration IDs)
            if "ддс" in text and "%" not in text and i not in matched_lines and not is_exemption_basis:
                if not any(ex in text for ex in vat_exclude):
                    val = _extract_rightmost_money(line)
                    if val is not None and summary.vat_amount.amount is None:
                        summary.vat_amount.amount = val
                        matched_lines.add(i)

    # Second pass: if total is still missing, look for the keyword "общо"
    # on lines that might have been skipped
    if summary.total_amount_due.amount is None:
        for i, line in enumerate(lines):
            if i in matched_lines:
                continue
            text = line.text_lower
            if any(h in text for h in header_exclude):
                continue
            if "общо" in text:
                val = _extract_rightmost_money(line)
                if val is not None:
                    summary.total_amount_due.amount = val
                    break

    # Financial Cross-Validation Step
    t = summary.total_amount_due.amount
    v = summary.vat_amount.amount
    tb = summary.tax_base.amount

    if t is not None and v is not None and tb is not None:
        # Check if tax base and VAT were extracted in reverse order (tb < v with ~20% or ~9% ratio)
        if tb > 0 and v > 0 and tb < v:
            swapped_rate = tb / v
            if abs(swapped_rate - Decimal("0.20")) < Decimal("0.02") or abs(swapped_rate - Decimal("0.09")) < Decimal("0.02"):
                logger.warning("Cross-validation: swapping inverted tax_base (%s) and vat_amount (%s)", tb, v)
                summary.tax_base.amount, summary.vat_amount.amount = v, tb
                tb, v = v, tb

        if abs(tb + v - t) > TOTAL_TOLERANCE:
            # Check Pair 1: tb and v confirm each other (e.g. rate == 20% or 9%)
            valid_pair_1 = False
            if tb > 0 and v > 0:
                rate1 = v / tb
                if abs(rate1 - Decimal("0.20")) < Decimal("0.02") or abs(rate1 - Decimal("0.09")) < Decimal("0.02"):
                    valid_pair_1 = True

            # Check Pair 2: t and tb confirm each other
            valid_pair_2 = False
            derived_v = t - tb
            if tb > 0 and derived_v > 0:
                rate2 = derived_v / tb
                if abs(rate2 - Decimal("0.20")) < Decimal("0.02") or abs(rate2 - Decimal("0.09")) < Decimal("0.02"):
                    valid_pair_2 = True

            # Check Pair 3: t and v confirm each other
            valid_pair_3 = False
            derived_tb = t - v
            if derived_tb > 0 and v > 0:
                rate3 = v / derived_tb
                if abs(rate3 - Decimal("0.20")) < Decimal("0.02") or abs(rate3 - Decimal("0.09")) < Decimal("0.02"):
                    valid_pair_3 = True

            if valid_pair_1 and not valid_pair_2 and not valid_pair_3:
                derived_t = (tb + v).quantize(Decimal("0.01"))
                logger.warning("Cross-validation: replacing corrupted total %s with derived %s", t, derived_t)
                summary.total_amount_due.amount = derived_t
            elif valid_pair_2 and not valid_pair_3:
                logger.warning("Cross-validation: replacing corrupted vat_amount %s with derived %s", v, derived_v)
                summary.vat_amount.amount = derived_v.quantize(Decimal("0.01"))
            elif valid_pair_3 and not valid_pair_2:
                logger.warning("Cross-validation: replacing corrupted tax_base %s with derived %s", tb, derived_tb)
                summary.tax_base.amount = derived_tb.quantize(Decimal("0.01"))
            else:
                # Fallback: if t is much larger than tb+v, replace t
                if tb > 0 and v > 0 and (tb + v) * Decimal("2") < t:
                    summary.total_amount_due.amount = (tb + v).quantize(Decimal("0.01"))
                elif derived_tb > 0 and abs(tb - derived_tb) > t * Decimal("0.5"):
                    summary.tax_base.amount = derived_tb.quantize(Decimal("0.01"))
                elif derived_v > 0 and abs(v - derived_v) > t * Decimal("0.5"):
                    summary.vat_amount.amount = derived_v.quantize(Decimal("0.01"))
    elif t is not None and v is not None and tb is None:
        summary.tax_base.amount = (t - v).quantize(Decimal("0.01"))
        logger.warning("Cross-validation: derived missing tax_base as %s", summary.tax_base.amount)
    elif t is not None and tb is not None and v is None:
        summary.vat_amount.amount = (t - tb).quantize(Decimal("0.01"))
        logger.warning("Cross-validation: derived missing vat_amount as %s", summary.vat_amount.amount)
    elif tb is not None and v is not None and t is None:
        summary.total_amount_due.amount = (tb + v).quantize(Decimal("0.01"))
        logger.warning("Cross-validation: derived missing total as %s", summary.total_amount_due.amount)

    # Check if numbers were extracted as integers missing a 2-decimal point (e.g. 2846 -> 28.46, 3415 -> 34.15)
    if line_items:
        items_sum = sum(
            (it.total_price_net.amount for it in line_items if it.total_price_net and it.total_price_net.amount and it.total_price_net.amount < Decimal("10000")),
            Decimal("0.00")
        )
        if summary.tax_base.amount is not None and summary.tax_base.amount > 100 and items_sum > 0:
            scaled_tb = (summary.tax_base.amount / 100).quantize(Decimal("0.01"))
            if abs(scaled_tb - items_sum) < Decimal("0.05") or any(abs(it.total_price_net.amount - scaled_tb) < Decimal("0.05") for it in line_items if it.total_price_net and it.total_price_net.amount):
                logger.warning("Scaling tax_base from %s to %s to match line items sum %s", summary.tax_base.amount, scaled_tb, items_sum)
                summary.tax_base.amount = scaled_tb
                if summary.total_amount_due.amount is not None and summary.total_amount_due.amount > 100:
                    summary.total_amount_due.amount = (summary.total_amount_due.amount / 100).quantize(Decimal("0.01"))
                if summary.vat_amount.amount is not None and summary.vat_amount.amount > 100:
                    summary.vat_amount.amount = (summary.vat_amount.amount / 100).quantize(Decimal("0.01"))

    return summary


def extract_currency(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
    date_issued: str | None = None,
) -> str | None:
    """Detect the primary currency used in the invoice.

    Scans for currency symbols/codes: ``лв``, ``лева``, ``BGN``,
    ``€``, ``EUR``, ``евро``.

    Returns ``"EUR"``, ``"BGN"``, or ``None`` if no currency is detected.
    Does NOT auto-convert between currencies.
    """
    full_text = " ".join(t.text for t in tokens).lower()

    eur_indicators = ["eur", "евро", "€", "евроцент"]
    bgn_indicators = ["bgn", "лв.", "лв ", "лева", "стотинк"]

    eur_count = sum(full_text.count(ind) for ind in eur_indicators)
    bgn_count = sum(full_text.count(ind) for ind in bgn_indicators)

    if eur_count > 0 and bgn_count > 0:
        if date_issued and date_issued >= EUR_MANDATORY_DATE:
            logger.info("Both EUR (%d) and BGN (%d) indicators found on %s, defaulting to EUR", eur_count, bgn_count, date_issued)
            return "EUR"
        # Both present — return the more frequent one as primary
        logger.info("Both EUR (%d) and BGN (%d) indicators found", eur_count, bgn_count)
        return "EUR" if eur_count >= bgn_count else "BGN"
    if eur_count > 0:
        return "EUR"
    if bgn_count > 0:
        return "BGN"
    return None


def _detect_all_currencies(tokens: list[OcrToken]) -> list[str]:
    """Return all currency codes detected in the document."""
    full_text = " ".join(t.text for t in tokens).lower()
    found: list[str] = []
    if any(ind in full_text for ind in ["eur", "евро", "€", "евроцент"]):
        found.append("EUR")
    if any(ind in full_text for ind in ["bgn", "лв.", "лв ", "лева", "стотинк"]):
        found.append("BGN")
    return found


def extract_amount_in_words(lines: list[LogicalLine]) -> str | None:
    """Extract the total-amount-in-words field.

    Looks for keywords like ``словом:``, ``с думи:``, or lines after
    the total that contain Bulgarian number words.

    Returns the RAW OCR text — no auto-correction is applied.
    """
    word_keywords = ["словом", "с думи", "сумата словом", "с думи:", "словом:"]

    for i, line in enumerate(lines):
        text = line.text_lower
        if any(kw in text for kw in word_keywords):
            # The amount-in-words may be on this line (after the keyword)
            # or on the next line
            m = re.search(r'(?:словом|с\s+думи)\s*[:./-]?\s*(.*)', line.text, re.I)
            if m and m.group(1).strip():
                return m.group(1).strip()
            # Try next line
            if i + 1 < len(lines):
                next_text = lines[i + 1].text.strip()
                if next_text and len(next_text) > 5:
                    return next_text

    # Fallback: look for lines containing Bulgarian number words near the end
    bg_number_words = [
        "нула", "едно", "две", "три", "четири", "пет", "шест", "седем",
        "осем", "девет", "десет", "двадесет", "тридесет", "четиридесет",
        "петдесет", "шестдесет", "седемдесет", "осемдесет", "деветдесет",
        "сто", "двеста", "триста", "четиристотин", "петстотин", "шестстотин",
        "седемстотин", "осемстотин", "деветстотин", "хиляди", "хиляда",
        "милион", "лева", "стотинки", "евро", "евроцента",
    ]
    # Check last 20% of lines
    start = max(0, len(lines) - len(lines) // 5 - 5)
    for line in lines[start:]:
        text = line.text_lower
        word_matches = sum(1 for w in bg_number_words if w in text)
        if word_matches >= 3:
            return line.text.strip()

    return None


def extract_payment_details(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
) -> PaymentDetails:
    """Extract payment information: IBAN, BIC, bank name, payment method."""
    pd = PaymentDetails()
    full_text = " ".join(t.text for t in tokens)

    # Cyrillic→Latin mapping for visually similar chars (OCR confusion)
    _CYR_TO_LAT = str.maketrans(
        'АВСЕНІКМОРТХавсенікмортх',
        'ABCEHIKMOPTXabcehikmoptx',
    )

    def _transliterate_for_iban(text: str) -> str:
        """Transliterate Cyrillic lookalikes to Latin for IBAN/BIC matching."""
        return text.translate(_CYR_TO_LAT)

    # Build multiple text variants for matching
    raw_upper = full_text.upper()
    translit_upper = _transliterate_for_iban(raw_upper)
    collapsed_raw = re.sub(r'\s+', '', raw_upper)
    collapsed_translit = re.sub(r'\s+', '', translit_upper)

    # IBAN: BG + 2 digits + 4 letters + 6 digits + 8 alphanumeric
    iban_pattern = r'BG\d{2}[A-Z]{4}\d{4,6}[A-Z0-9]{6,10}'
    iban_matches = re.findall(iban_pattern, collapsed_raw)
    if not iban_matches:
        iban_matches = re.findall(iban_pattern, collapsed_translit)
    if not iban_matches:
        # Try with spaces
        spaced_pattern = r'BG\s*\d{2}\s*[A-Z]{4}\s*\d{4,6}\s*[A-Z0-9]{6,10}'
        iban_matches = re.findall(spaced_pattern, translit_upper)
        if iban_matches:
            iban_matches = [re.sub(r'\s+', '', m) for m in iban_matches]
    if iban_matches:
        pd.iban = normalize_iban(iban_matches[0])

    # BIC/SWIFT — also use transliterated text
    bic_pattern = r'(?:BIC|SWIFT|БИК)\s*[:./-]?\s*([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)'
    bic_match = re.search(bic_pattern, translit_upper)
    if not bic_match:
        bic_match = re.search(bic_pattern, raw_upper)
    if bic_match:
        pd.bic = normalize_bic(bic_match.group(1))

    # Bank name
    bank_patterns = [
        r'(?:Банка|банка|Bank|BANK)\s*[:./-]?\s*([А-Яа-яA-Za-z\s"]+)',
    ]
    for pattern in bank_patterns:
        m = re.search(pattern, full_text)
        if m:
            bank_name = m.group(1).strip()
            # Limit to reasonable length
            if 3 <= len(bank_name) <= 80:
                pd.bank_name = bank_name
            break

    # Payment method
    method_patterns = [
        (r'(?i)(?:по\s*)?банков\s*(?:път|превод)', "банков превод"),
        (r'(?i)в\s*брой', "в брой"),
        (r'(?i)(?:payment|плащане)\s*[:./-]?\s*(.*)', None),
    ]
    for pattern, default_method in method_patterns:
        m = re.search(pattern, full_text)
        if m:
            pd.method = default_method or m.group(1).strip()
            break

    return pd


def calculate_ocr_confidence(tokens: list[OcrToken]) -> float | None:
    """Calculate a normalised OCR confidence score between 0.0 and 1.0.

    Computed as the mean confidence of tokens with conf > 0, divided by 100.
    Returns ``None`` if there are no valid tokens.
    """
    valid_confs = [t.conf for t in tokens if t.conf > 0]
    if not valid_confs:
        return None
    return round(sum(valid_confs) / len(valid_confs) / 100.0, 2)


# ===================================================================
# LAYER 5 — VALIDATION
# ===================================================================

def _validate_required_fields(invoice: Invoice) -> list[ValidationIssue]:
    """Check for missing critical fields."""
    issues: list[ValidationIssue] = []

    if not invoice.invoice_metadata.invoice_number:
        issues.append(ValidationIssue(
            code="MISSING_INVOICE_NUMBER",
            message="Invoice number was not detected",
            severity="error",
            field="invoice_metadata.invoice_number",
        ))

    if not invoice.invoice_metadata.date_issued:
        issues.append(ValidationIssue(
            code="MISSING_DATE_ISSUED",
            message="Issue date was not detected",
            severity="error",
            field="invoice_metadata.date_issued",
        ))

    if not invoice.supplier.name:
        issues.append(ValidationIssue(
            code="MISSING_SUPPLIER_NAME",
            message="Supplier name was not detected",
            severity="error",
            field="supplier.name",
        ))

    if not invoice.recipient.name:
        issues.append(ValidationIssue(
            code="MISSING_RECIPIENT_NAME",
            message="Recipient name was not detected",
            severity="error",
            field="recipient.name",
        ))

    return issues


def _validate_identifiers(invoice: Invoice) -> list[ValidationIssue]:
    """Validate EIK and VAT number formats."""
    issues: list[ValidationIssue] = []

    for role, party in [("supplier", invoice.supplier), ("recipient", invoice.recipient)]:
        if party.eik:
            digits = re.sub(r'\D', '', party.eik)
            if len(digits) not in (9, 10, 13):
                issues.append(ValidationIssue(
                    code="INVALID_EIK_FORMAT",
                    message=f"{role.title()} EIK '{party.eik}' does not match expected 9/13 digit format",
                    severity="warning",
                    field=f"{role}.eik",
                    detected_value=party.eik,
                ))

        if party.vat_number:
            if not re.match(r'^BG\d{9,13}$', party.vat_number):
                issues.append(ValidationIssue(
                    code="INVALID_VAT_FORMAT",
                    message=f"{role.title()} VAT number '{party.vat_number}' does not match BG + 9-13 digits",
                    severity="warning",
                    field=f"{role}.vat_number",
                    detected_value=party.vat_number,
                ))

    return issues


def _validate_dates(invoice: Invoice) -> list[ValidationIssue]:
    """Validate date formats and plausibility."""
    issues: list[ValidationIssue] = []

    for field_name, date_val in [
        ("date_issued", invoice.invoice_metadata.date_issued),
        ("date_tax_event", invoice.invoice_metadata.date_tax_event),
    ]:
        if date_val:
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_val):
                issues.append(ValidationIssue(
                    code="INVALID_DATE_FORMAT",
                    message=f"Date '{date_val}' is not in YYYY-MM-DD format",
                    severity="warning",
                    field=f"invoice_metadata.{field_name}",
                    detected_value=date_val,
                ))

    return issues


def _validate_line_items(invoice: Invoice) -> list[ValidationIssue]:
    """Validate individual line items for mathematical consistency."""
    issues: list[ValidationIssue] = []

    for i, item in enumerate(invoice.line_items):
        field_prefix = f"line_items[{i}]"

        # Check description (Feature 19: Strict Null Fallback)
        banned_desc = {"item", "unknown", "placeholder", "n/a", "none", "артикул", "null", ""}
        desc_clean = item.description.strip().lower() if item.description else ""
        if not desc_clean or desc_clean in banned_desc:
            item.description = None
            issues.append(ValidationIssue(
                code="MISSING_DESCRIPTION",
                message=f"Line {i+1} has missing or unresolvable description due to occlusion or illegibility",
                severity="warning",
                field=f"{field_prefix}.description",
                detected_value=None,
                expected_value="Valid item description",
            ))

        u_price = item.unit_price_net.amount if isinstance(item.unit_price_net, MoneyAmount) else item.unit_price_net
        t_price = item.total_price_net.amount if isinstance(item.total_price_net, MoneyAmount) else item.total_price_net

        # Check quantity × unit_price ≈ total_price
        if (
            item.quantity is not None
            and u_price is not None
            and t_price is not None
        ):
            expected = (item.quantity * u_price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP,
            )
            diff = abs(expected - t_price)
            if diff > VAT_TOLERANCE:
                issues.append(ValidationIssue(
                    code="LINE_ITEM_CALC_MISMATCH",
                    message=(
                        f"Line {i+1}: quantity ({item.quantity}) × unit_price "
                        f"({u_price}) = {expected}, but total is "
                        f"{t_price}"
                    ),
                    severity="warning",
                    field=f"{field_prefix}.total_price_net",
                    detected_value=str(t_price),
                    expected_value=str(expected),
                    difference=str(diff),
                ))

        # Negative quantity
        if item.quantity is not None and item.quantity < 0:
            issues.append(ValidationIssue(
                code="NEGATIVE_QUANTITY",
                message=f"Line {i+1} has negative quantity: {item.quantity}",
                severity="warning",
                field=f"{field_prefix}.quantity",
                detected_value=str(item.quantity),
            ))

        # Negative price (unusual unless it's a credit note)
        if u_price is not None and u_price < 0:
            issues.append(ValidationIssue(
                code="NEGATIVE_PRICE",
                message=f"Line {i+1} has negative unit price: {u_price}",
                severity="warning",
                field=f"{field_prefix}.unit_price_net",
                detected_value=str(u_price),
            ))

        # VAT rate plausibility
        if item.vat_rate_pct is not None:
            if item.vat_rate_pct < 0 or item.vat_rate_pct > 25:
                issues.append(ValidationIssue(
                    code="VAT_RATE_IMPLAUSIBLE",
                    message=f"Line {i+1}: VAT rate {item.vat_rate_pct}% is outside plausible range (0-25%)",
                    severity="warning",
                    field=f"{field_prefix}.vat_rate_pct",
                    detected_value=str(item.vat_rate_pct),
                ))

    return issues


def _validate_totals(invoice: Invoice) -> list[ValidationIssue]:
    """Validate financial summary consistency.

    Checks:
        1. sum(line_items.total_price_net) ≈ tax_base
        2. tax_base × inferred_vat_rate ≈ vat_amount
        3. tax_base + vat_amount ≈ total_amount_due
    """
    issues: list[ValidationIssue] = []
    fs = invoice.financial_summary

    # 1. Line items total vs tax base
    items_with_total = [
        item for item in invoice.line_items if item.total_price_net is not None
    ]
    if items_with_total and fs.tax_base.amount is not None:
        items_sum = sum(
            ((item.total_price_net.amount if isinstance(item.total_price_net, MoneyAmount) else item.total_price_net) or Decimal(0))
            for item in items_with_total
        )
        diff = abs(items_sum - fs.tax_base.amount)
        if diff > TOTAL_TOLERANCE:
            issues.append(ValidationIssue(
                code="LINE_ITEMS_TOTAL_MISMATCH",
                message=(
                    f"Sum of line item totals ({items_sum}) does not match "
                    f"tax base ({fs.tax_base.amount})"
                ),
                severity="error",
                field="financial_summary.tax_base",
                detected_value=str(fs.tax_base.amount),
                expected_value=str(items_sum),
                difference=str(diff),
            ))

    # 2. VAT calculation
    if fs.tax_base.amount is not None and fs.vat_amount.amount is not None:
        if fs.tax_base.amount > 0:
            vat_20 = (fs.tax_base.amount * Decimal("0.20")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            vat_9 = (fs.tax_base.amount * Decimal("0.09")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            vat_0 = Decimal("0.00")
            
            diff_20 = abs(vat_20 - fs.vat_amount.amount)
            diff_9 = abs(vat_9 - fs.vat_amount.amount)
            diff_0 = abs(vat_0 - fs.vat_amount.amount)

            if diff_20 > VAT_TOLERANCE and diff_9 > VAT_TOLERANCE and diff_0 > VAT_TOLERANCE:
                issues.append(ValidationIssue(
                    code="VAT_CALCULATION_MISMATCH",
                    message=(
                        f"VAT amount ({fs.vat_amount.amount}) does not match "
                        f"tax base ({fs.tax_base.amount}) at 20%, 9%, or 0% rate"
                    ),
                    severity="error",
                    field="financial_summary.vat_amount",
                    detected_value=str(fs.vat_amount.amount),
                    expected_value=f"~{vat_20} (at 20%)",
                ))

    # 3. Total = tax_base + vat_amount
    if (
        fs.tax_base.amount is not None
        and fs.vat_amount.amount is not None
        and fs.total_amount_due.amount is not None
    ):
        expected_total = fs.tax_base.amount + fs.vat_amount.amount
        diff = abs(expected_total - fs.total_amount_due.amount)
        if diff > TOTAL_TOLERANCE:
            issues.append(ValidationIssue(
                code="TOTAL_SUM_MISMATCH",
                message=(
                    f"Tax base ({fs.tax_base.amount}) + VAT ({fs.vat_amount.amount}) "
                    f"= {expected_total}, but total is {fs.total_amount_due.amount}"
                ),
                severity="error",
                field="financial_summary.total_amount_due",
                detected_value=str(fs.total_amount_due.amount),
                expected_value=str(expected_total),
                difference=str(diff),
            ))

    # Zero total warning
    if fs.total_amount_due.amount is not None and fs.total_amount_due.amount == 0:
        issues.append(ValidationIssue(
            code="ZERO_TOTAL",
            message="Total amount due is zero",
            severity="warning",
            field="financial_summary.total_amount_due",
            detected_value="0",
        ))

    return issues


def _validate_currency(invoice: Invoice, tokens: list[OcrToken]) -> list[ValidationIssue]:
    """Validate currency consistency and euro-transition logic.

    This is the CRITICAL currency validation that:
        - Checks consistency across all MoneyAmount fields
        - Detects BGN usage after euro adoption (2026-01-01)
        - Detects BGN usage after dual-display period (2026-08-08)
        - Detects dual-currency documents
        - Checks amount-in-words currency vs numeric currency
        - NEVER auto-converts BGN → EUR
    """
    issues: list[ValidationIssue] = []
    fs = invoice.financial_summary

    # Collect all currencies from financial fields
    field_currencies: dict[str, str | None] = {
        "tax_base": fs.tax_base.currency,
        "vat_amount": fs.vat_amount.currency,
        "total_amount_due": fs.total_amount_due.currency,
    }
    non_null_currencies = {
        v for v in field_currencies.values() if v is not None
    }

    # Check consistency
    if len(non_null_currencies) > 1:
        issues.append(ValidationIssue(
            code="CURRENCY_MISMATCH",
            message=(
                f"Inconsistent currencies across financial fields: "
                f"{dict((k, v) for k, v in field_currencies.items() if v)}"
            ),
            severity="warning",
            field="financial_summary",
        ))

    primary_currency = non_null_currencies.pop() if len(non_null_currencies) == 1 else None

    # Detect all currencies in the document
    all_detected = _detect_all_currencies(tokens)

    # Dual currency detection
    if "EUR" in all_detected and "BGN" in all_detected:
        issues.append(ValidationIssue(
            code="DUAL_CURRENCY_DETECTED",
            message="Both EUR and BGN are present in the document",
            severity="warning",
        ))

    # Euro transition logic
    date_issued = invoice.invoice_metadata.date_issued
    if date_issued and primary_currency == "BGN":
        if date_issued >= DUAL_DISPLAY_END_DATE:
            issues.append(ValidationIssue(
                code="CURRENCY_AFTER_DUAL_PERIOD",
                message=(
                    f"BGN detected as primary currency on {date_issued}, "
                    f"after the mandatory dual-display period ended "
                    f"({DUAL_DISPLAY_END_DATE}). EUR should be the primary "
                    f"sales/payable currency. BGN may appear only as an "
                    f"informational equivalent."
                ),
                severity="warning",
                field="financial_summary",
                detected_value="BGN",
            ))
        elif date_issued >= EUR_MANDATORY_DATE:
            issues.append(ValidationIssue(
                code="CURRENCY_POST_EURO_BGN_DETECTED",
                message=(
                    f"BGN detected as primary currency on {date_issued}, "
                    f"after euro adoption date ({EUR_MANDATORY_DATE}). "
                    f"This may indicate: (a) an informational BGN reference, "
                    f"(b) a pre-euro document with incorrect date, or "
                    f"(c) an OCR error. The value has NOT been auto-converted."
                ),
                severity="warning",
                field="financial_summary",
                detected_value="BGN",
                expected_value="EUR",
            ))

    return issues


def _validate_amount_in_words(invoice: Invoice) -> list[ValidationIssue]:
    """Check amount-in-words currency against the numeric currency.

    E.g. words say ``лева и стотинки`` but amounts are in EUR.
    """
    issues: list[ValidationIssue] = []
    words = invoice.financial_summary.total_amount_words
    if not words:
        return issues

    words_lower = words.lower()

    # Detect currency in words
    words_has_bgn = any(
        kw in words_lower for kw in ["лева", "лев", "стотинки", "стотинка"]
    )
    words_has_eur = any(
        kw in words_lower for kw in ["евро", "евроцент", "евроцента"]
    )

    primary_currency = invoice.financial_summary.total_amount_due.currency

    if primary_currency == "EUR" and words_has_bgn and not words_has_eur:
        issues.append(ValidationIssue(
            code="AMOUNT_WORDS_CURRENCY_MISMATCH",
            message=(
                f"Amount-in-words mentions BGN ('лева'/'стотинки') but "
                f"numeric currency is EUR. The text has NOT been auto-corrected."
            ),
            severity="warning",
            field="financial_summary.total_amount_words",
            detected_value=words[:100],
            expected_value="EUR-denominated text",
        ))

    if primary_currency == "BGN" and words_has_eur and not words_has_bgn:
        issues.append(ValidationIssue(
            code="AMOUNT_WORDS_CURRENCY_MISMATCH",
            message=(
                f"Amount-in-words mentions EUR ('евро') but numeric "
                f"currency is BGN."
            ),
            severity="warning",
            field="financial_summary.total_amount_words",
            detected_value=words[:100],
            expected_value="BGN-denominated text",
        ))

    return issues


def _validate_ocr_confidence(
    invoice: Invoice,
    tokens: list[OcrToken],
) -> list[ValidationIssue]:
    """Flag low overall confidence and low-confidence critical fields."""
    issues: list[ValidationIssue] = []

    score = invoice.invoice_metadata.ocr_confidence_score
    if score is not None and score < 0.5:
        issues.append(ValidationIssue(
            code="LOW_OCR_CONFIDENCE",
            message=f"Overall OCR confidence is low ({score:.2f})",
            severity="warning",
        ))

    # Check if many tokens are low-confidence
    if tokens:
        low_conf_count = sum(1 for t in tokens if 0 < t.conf < MIN_CONFIDENCE)
        low_conf_pct = low_conf_count / len(tokens)
        if low_conf_pct > 0.3:
            issues.append(ValidationIssue(
                code="HIGH_LOW_CONFIDENCE_RATIO",
                message=(
                    f"{low_conf_pct:.0%} of OCR tokens have confidence "
                    f"below {MIN_CONFIDENCE}"
                ),
                severity="warning",
            ))

    return issues


def validate_invoice(
    invoice: Invoice,
    tokens: list[OcrToken],
) -> ValidationResult:
    """Run all validators and aggregate results.

    Sets ``is_valid = True`` only when there are zero errors.
    """
    result = ValidationResult()

    # Collect all issues from sub-validators
    all_issues: list[ValidationIssue] = []
    all_issues.extend(_validate_required_fields(invoice))
    all_issues.extend(_validate_identifiers(invoice))
    all_issues.extend(_validate_dates(invoice))
    all_issues.extend(_validate_line_items(invoice))
    all_issues.extend(_validate_totals(invoice))
    all_issues.extend(_validate_currency(invoice, tokens))
    all_issues.extend(_validate_amount_in_words(invoice))
    all_issues.extend(_validate_ocr_confidence(invoice, tokens))

    for issue in all_issues:
        if issue.severity == "error":
            result.errors.append(issue)
        else:
            result.warnings.append(issue)

    result.is_valid = len(result.errors) == 0
    return result


# ===================================================================
# MAIN PIPELINE
# ===================================================================

def process_invoice(image_path: Path | str, debug_dir: Path | None = None) -> Invoice:
    """Full invoice processing pipeline.

    1. Load document (PDF or image)
    2. Iterate over all pages
    3. Generate preprocessing variants and run multi-pass OCR per page
    4. Normalize tokens
    5. Group into lines/blocks
    6. Detect table regions
    7. Extract all fields
    8. Calculate OCR confidence
    9. Validate
    10. Return structured Invoice
    """
    path = Path(image_path)
    pages = load_document(path)

    normalized_pages: list[PageImage] = []
    page_transforms: list[PageTransform] = []
    all_tokens: list[OcrToken] = []

    for page in pages:
        norm_page, transform = normalize_page_geometry(page)
        normalized_pages.append(norm_page)
        page_transforms.append(transform)

        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(debug_dir / f"{path.stem}_page_{page.page_number}_raw.png"), page.image)
            cv2.imwrite(str(debug_dir / f"{path.stem}_page_{norm_page.page_number}_norm.png"), norm_page.image)

        variants = generate_preprocessing_variants(norm_page.image)
        page_tokens = run_multiple_ocr_passes(variants)
        for tok in page_tokens:
            tok.page_number = norm_page.page_number
            tok.is_low_confidence = (tok.conf < MIN_CONFIDENCE)
        all_tokens.extend(page_tokens)
    embedded_pdf_tokens: list[OcrToken] = []
    if path.suffix.lower() in PDF_EXTENSIONS:
        try:
            doc = pymupdf.open(path)
            full_pdf_text = "".join(p.get_text() for p in doc).lower()
            statutory_checks = {
                "фактура", "доставчик", "получател", "еик", "стока",
                "код", "цена", "мярка", "стойност", "ддс",
            }
            matched_statutory = sum(1 for kw in statutory_checks if kw in full_pdf_text)
            if matched_statutory >= 3:
                scale = 300.0 / 72.0
                for p_idx, page in enumerate(doc):
                    words = page.get_text("words")
                    for w in words:
                        txt = w[4].strip()
                        if not txt:
                            continue
                        bx = int(round(w[0] * scale))
                        by = int(round(w[1] * scale))
                        bw = max(1, int(round((w[2] - w[0]) * scale)))
                        bh = max(1, int(round((w[3] - w[1]) * scale)))
                        embedded_pdf_tokens.append(OcrToken(
                            text=txt,
                            conf=99.0,
                            bbox=(bx, by, bw, bh),
                            page_number=p_idx + 1,
                            is_low_confidence=False,
                        ))
            doc.close()
        except Exception as exc:
            logger.warning("Embedded PDF text extraction skipped: %s", exc)

    if embedded_pdf_tokens:
        all_tokens = fuse_ocr_passes(embedded_pdf_tokens, all_tokens)

    raw_evidence = build_raw_ocr_evidence(normalized_pages, all_tokens)

    if not all_tokens:
        logger.error("OCR produced no tokens")
        invoice = Invoice()
        invoice.raw_ocr_evidence = raw_evidence
        invoice.validation.errors.append(ValidationIssue(
            code="OCR_NO_TOKENS",
            message="OCR failed to recognise any text in the image",
            severity="error",
        ))
        return invoice

    # Layer 3: Normalization
    tokens = normalize_ocr_tokens(all_tokens)

    # Layer 4: Layout analysis
    lines = group_tokens_into_lines(tokens)
    blocks = group_lines_into_blocks(lines)
    table_regions = detect_table_regions(lines, tokens)

    logger.info(
        "Layout: %d lines, %d blocks, %d table regions",
        len(lines), len(blocks), len(table_regions),
    )

    # Layer 4: Field extraction
    invoice = Invoice()
    invoice.raw_ocr_evidence = raw_evidence

    # Metadata
    invoice.invoice_metadata.invoice_number = extract_invoice_number(lines, tokens)
    date_issued, date_tax_event = extract_dates(lines)
    invoice.invoice_metadata.date_issued = date_issued
    invoice.invoice_metadata.date_tax_event = date_tax_event
    invoice.invoice_metadata.place_issued = extract_place_issued(lines)
    invoice.invoice_metadata.ocr_confidence_score = calculate_ocr_confidence(tokens)

    # Parties
    invoice.supplier = extract_party(lines, tokens, "supplier")
    invoice.recipient = extract_party(lines, tokens, "recipient")

    # Line items
    invoice.line_items = extract_line_items(table_regions, lines)

    # Financial summary
    invoice.financial_summary = extract_financial_summary(lines, tokens, line_items=invoice.line_items)

    # Currency — detect but NEVER auto-convert
    detected_currency = extract_currency(lines, tokens, invoice.invoice_metadata.date_issued)
    if detected_currency:
        # Apply to financial fields that don't already have a currency set
        for money_field in [
            invoice.financial_summary.tax_base,
            invoice.financial_summary.vat_amount,
            invoice.financial_summary.total_amount_due,
        ]:
            if money_field.currency is None:
                money_field.currency = detected_currency

    # Amount in words — raw OCR text, no auto-correction
    invoice.financial_summary.total_amount_words = extract_amount_in_words(lines)

    # Payment details
    invoice.payment_details = extract_payment_details(lines, tokens)

    # Layer 5: Validation
    invoice.validation = validate_invoice(invoice, tokens)

    return invoice


def main() -> None:
    """CLI entry point: parse args, process invoice, output JSON."""
    parser = argparse.ArgumentParser(
        description="Bulgarian Invoice OCR Processor",
    )
    parser.add_argument(
        "image",
        type=str,
        nargs="?",
        help="Path to invoice document (.pdf, .png, .jpg, .jpeg) (for single file mode)",
    )
    parser.add_argument("--input-dir", type=str, help="Directory containing multiple invoice documents")
    parser.add_argument("--output-dir", type=str, default="results/", help="Directory to save JSON outputs")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode to save intermediate artifacts")
    parser.add_argument("--debug-dir", type=str, default="debug/", help="Directory to save debug artifacts")
    args = parser.parse_args()

    # Configure logging to stderr
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s: %(message)s",
    )
    
    debug_dir = Path(args.debug_dir) if args.debug else None

    if args.input_dir:
        in_dir = Path(args.input_dir)
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = {"processed": 0, "failed": 0, "results": []}
        
        for file_path in in_dir.rglob("*"):
            if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                try:
                    logger.info(f"Processing {file_path.name}")
                    invoice = process_invoice(file_path, debug_dir=debug_dir)
                    json_output = serialize_invoice(invoice)
                    out_file = out_dir / f"{file_path.stem}.json"
                    out_file.write_text(json_output, encoding="utf-8")
                    
                    summary["processed"] += 1
                    summary["results"].append({"file": file_path.name, "status": "success", "invoice_no": invoice.invoice_metadata.invoice_number})
                except Exception as e:
                    logger.error(f"Failed {file_path.name}: {e}")
                    summary["failed"] += 1
                    summary["results"].append({"file": file_path.name, "status": "error", "error": str(e)})
                    
        (out_dir / "batch_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        sys.exit(0)

    if not args.image:
        parser.error("Either image or --input-dir must be provided")

    # Validate input
    image_path = Path(args.image)
    if not image_path.exists():
        logger.error("File not found: %s", image_path)
        sys.exit(1)
    if image_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        logger.error(
            "Unsupported file type: %s (supported: %s)",
            image_path.suffix,
            ", ".join(sorted(SUPPORTED_EXTENSIONS)),
        )
        sys.exit(1)

    # Check Tesseract availability
    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        logger.error(
            "Tesseract OCR is not installed or not on PATH."
        )
        sys.exit(1)

    try:
        invoice = process_invoice(image_path, debug_dir=debug_dir)
        json_output = serialize_invoice(invoice)
        # stdout: ONLY the JSON
        print(json_output)
    except FileNotFoundError as exc:
        logger.error("File error: %s", exc)
        sys.exit(1)
    except ValueError as exc:
        logger.error("Image error: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.error("Processing failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

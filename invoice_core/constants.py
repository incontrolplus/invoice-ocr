"""Constants and configuration settings for Bulgarian Invoice OCR Pipeline."""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
import re

# Logging
import logging
logger = logging.getLogger("invoice_ocr")

# Default Tesseract OCR language(s) — combined Bulgarian + English.
DEFAULT_OCR_LANG: str = "bul+eng"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Candidate directories to discover tessdata containing bul.traineddata
KNOWN_TESSDATA_LOCATIONS: tuple[Path, ...] = (
    PROJECT_ROOT / "tessdata",
    Path(__file__).resolve().parent / "tessdata",
    Path("/opt/homebrew/share/tessdata"),
    Path("/usr/local/share/tessdata"),
    Path("/usr/share/tesseract-ocr/5/tessdata"),
    Path("/usr/share/tesseract-ocr/4.00/tessdata"),
    Path("/usr/share/tessdata"),
    Path("C:/Program Files/Tesseract-OCR/tessdata"),
)

PDF_EXTENSIONS: set[str] = {".pdf"}
IMAGE_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg"}
SUPPORTED_EXTENSIONS: set[str] = {".pdf", ".png", ".jpg", ".jpeg"}

# Default rasterization resolution (empirically optimized for Tesseract)
DEFAULT_RASTER_DPI: int = 300

# Default directory for persistent OCR token cache
DEFAULT_OCR_CACHE_DIR: Path = Path(".ocr_cache")

# OCR confidence threshold — tokens below this are kept but flagged
MIN_CONFIDENCE: int = 60

BULGARIAN_KEYWORDS: set[str] = {
    "фактура", "доставчик", "получател", "еик", "ддс", "данъчна", "основа",
    "стойност", "сума", "общо", "бг", "банка", "ибан", "лева", "лв", "евро",
    "eur", "bgn", "място", "издаване", "дата", "оригинал", "клиент", "купувач",
    "продавач", "капина", "плевен", "софия", "телефон", "мол", "адрес", "стока",
    "номер", "цена", "мярка", "количество", "плащане", "сметка", "бик", "swift",
}

# Fixed EUR/BGN conversion rate (official, irrevocable)
FIXED_EUR_BGN_RATE: Decimal = Decimal("1.95583")

# Euro adoption date in Bulgaria
EUR_MANDATORY_DATE: str = "2026-01-01"

# End of mandatory dual-display period
DUAL_DISPLAY_END_DATE: str = "2026-08-08"

# Tolerance for financial validation (accounts for rounding)
VAT_TOLERANCE: Decimal = Decimal("0.02")
TOTAL_TOLERANCE: Decimal = Decimal("0.02")
ZDDS_DISCOUNT_TOLERANCE: Decimal = Decimal("0.03")  # Art. 26 ZDDS volume discounts & cumulative row rounding
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
        "нек-во", "мее-во", "мее-ва", "нек-ва", "мек-во", "мек-ва",
        "колич.", "колич", "коляч.", "коляч", "колик.", "колик",
    ],
    "packaging": [
        "съд/бр", "съд/бр.", "съдйбр.", "съдйбр", "съдйбр,", "съдйвр",
        "разфасовка", "разф.", "разф", "съдържание в брой", "съдържание", "опаковка", "съд",
        "в каси", "м/к-я", "м/кж",
    ],
    "unit": [
        "мерна единица", "ед. мярка", "ед.м.", "ед. м.", "м.ед.", "м. ед.",
        "мярка", "м-ка", "единица", "ед.", "марка", "мярна", "unit", "uom",
    ],
    "unit_price": [
        "цена без ддс", "единична цена", "цена за ед.", "цена за ед", "цена за единица",
        "ед. цена", "ед.цена", "ед цена", "цена нето", "ед. с-ст", "ед.стойност",
        "unit price", "цена", "price", "ед, цева", "ед, цена", "ед. цева",
        "ea. цена", "es. цева", "ea. цева", "es. цена",
        "ед.цена-ст.", "ед.цена-ст", "ед. цена-ст.", "ед. цена-ст", "едиенаста", "ед.ценжст.", "ед.ценжст", "ед. ценж",
    ],
    "total_price": [
        "стойност без ддс", "сума без ддс", "обща стойност без ддс", "обща стойност",
        "обща сума без ддс", "общо без ддс", "стойност нето", "сума нето",
        "данъчна основа", "стойност", "сума", "нето", "ст-ст.", "ст-ст",
        "с-ст", "стоиност", "стойносг", "стойпост", "стойн", "amount", "total", "net amount",
        "сува вето", "сума вето", "сува нето", "гуна дас", "сума дас",
        "стой-ст eur", "стой-ст.", "стой-ст", "стои-ст", "стей-ст", "стет eur", "стет",
    ],
    "vat_rate": [
        "ддс ставка", "данъчна ставка", "ставка ддс", "ддс %", "ддс%",
        "ставка %", "ставка", "данък %", "% ддс", "%ддс", "ддс", "vat %", "vat",
        "ддс 90)", "ддс 90", "ддс 96",
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
    re.compile(r'^\s*(?:пренос|от\s+пренос|към\s+пренос|за\s+пренасяне|пренесено|пренесен\s+остатък|пренесена\s+сума|сума\s+за\s+пренасяне|междинна\s+сума|междинен\s+сбор)\b', re.IGNORECASE),
    re.compile(r'\b(?:посл\.\s*стр\.\s*общо|посл\.\s*стр\.|стр\.\s*общо)\b', re.IGNORECASE),
    re.compile(r'\b(?:пренос\s+(?:от|към|на)\s+следваща\s+страница|пренос\s+от\s+предходна\s+страница)\b', re.IGNORECASE),
    re.compile(r'^\s*(?:продължение(?:\s+на\s+следваща\s+страница)?|страница\s*\d+|\bстр\.\s*\d+)\s*$', re.IGNORECASE),
    re.compile(r'^\s*(?:общо\s+нето|око\s+нето|общо\s+ддс|всичко\s+общо)\b', re.IGNORECASE),
]

CONTINUATION_KEYWORDS: list[str] = [
    "пренос", "от пренос", "към пренос", "за пренасяне", "пренесено", "пренесена сума",
    "сума за пренасяне", "посл. стр. общо", "посл. стр.", "стр. общо", "продължение",
    "пренесен остатък", "междинна сума", "междинен сбор",
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

SUPPLIER_KEYWORD_RE: re.Pattern = re.compile(
    r'(?i)\b(?:[гГ]?[дd][оo][сc]?[тt]ав[а-яA-Za-z]{0,6}|продавач|изпълнител|supplier)\b'
)
RECIPIENT_KEYWORD_RE: re.Pattern = re.compile(
    r'(?i)\b(?:[пП][оОуУ]?[лЛпП]?[уУ][чч][а-яA-Za-z]{2,8}|кзлуч[а-яA-Za-z]{2,8}|пучкт[а-яA-Za-z]{2,8}|купувач|клиент|възложител|recipient)\b'
)


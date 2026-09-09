"""Data models and dataclasses for Bulgarian Invoice OCR Pipeline."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
import json
import re
from typing import Any

import numpy as np

from .constants import MIN_CONFIDENCE
from .currency import convert_eur_to_bgn, convert_bgn_to_eur

@dataclass
class PageImage:
    """Represents an ingested document page rendered as an OpenCV BGR image."""
    page_number: int = 1  # 1-indexed (1, 2, ...)
    image: np.ndarray | None = None  # BGR format uint8 numpy array, or None if image memory is released
    width: int = 0         # Image width in pixels
    height: int = 0        # Image height in pixels


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
    article_code: str | None = None
    page_number: int = 1
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)

    @property
    def sku(self) -> str | None:
        return self.article_code

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
    total_amount_bgn: MoneyAmount | None = None
    total_amount_eur: MoneyAmount | None = None
    dual_display_total: MoneyAmount | None = None


@dataclass
class PaymentDetails:
    """Payment information."""
    method: str | None = None
    bank_name: str | None = None
    iban: str | None = None
    bic: str | None = None
    due_date: str | None = None  # YYYY-MM-DD
    bank_code: str | None = None
    is_iban_valid: bool | None = None
    is_bic_valid: bool | None = None
    bank_recognized: bool = False


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
class BankComplianceDetails:
    """Bank requisites extraction and validation details under Bulgarian banking standards."""
    iban: str | None = None
    bic: str | None = None
    bank_name: str | None = None
    bank_code: str | None = None
    is_iban_valid: bool = False
    is_bic_valid: bool = False
    bank_recognized: bool = False
    servicing_bank: str | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class VatRegimeComplianceDetails:
    """Statutory VAT regime validation details under VAT Act (ЗДДС чл. 114, 113, 86, 163а, 141, 28, etc.)."""
    vat_rate_pct: Decimal | None = None
    vat_amount: Decimal | None = None
    tax_base: Decimal | None = None
    is_zero_or_exempt: bool = False
    legal_basis_code: str | None = None
    legal_basis_article: str | None = None
    legal_basis_text: str | None = None
    regime_name: str | None = None
    is_valid_basis: bool = False
    issues: list[str] = field(default_factory=list)


@dataclass
class SignatoriesComplianceDetails:
    """Compiler and representative verification under Art. 6(1)(5) Accountancy Act (ЗСч)."""
    compiled_by: str | None = None
    supplier_mol: str | None = None
    recipient_mol: str | None = None
    received_by: str | None = None
    is_compliant: bool = False
    issues: list[str] = field(default_factory=list)


@dataclass
class LegalComplianceReport:
    """Specialized statutory legal and tax compliance audit report."""
    is_compliant: bool = False
    zsch_compliant: bool = False
    zdds_compliant: bool = False
    bank_requisites: BankComplianceDetails = field(default_factory=BankComplianceDetails)
    vat_regime: VatRegimeComplianceDetails = field(default_factory=VatRegimeComplianceDetails)
    signatories: SignatoriesComplianceDetails = field(default_factory=SignatoriesComplianceDetails)
    mandatory_requisites_check: dict[str, bool] = field(default_factory=dict)
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Aggregated validation outcome."""
    is_valid: bool = False
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    legal_compliance_report: LegalComplianceReport | None = None


class DocumentType(str, Enum):
    """Bulgarian financial document types."""
    INVOICE = "INVOICE"
    CREDIT_NOTE = "CREDIT_NOTE"
    DEBIT_NOTE = "DEBIT_NOTE"
    PAYMENT_ORDER_NAP = "PAYMENT_ORDER_NAP"
    FISCAL_RECEIPT = "FISCAL_RECEIPT"
    PROTOCOL_CHL_117 = "PROTOCOL_CHL_117"
    FISCAL_MEMORY_REPORT = "FISCAL_MEMORY_REPORT"
    GOODS_RECEIPT = "GOODS_RECEIPT"


@dataclass
class DocumentClassificationResult:
    """Outcome of preliminary document classification."""
    document_type: DocumentType
    confidence: float
    matched_rule: str
    matched_keywords: list[str] = field(default_factory=list)


@dataclass
class BudgetPaymentDetails:
    """Specialized model for budget payment orders and NAP tax/social security contributions."""
    nra_iban: str | None = None
    obligated_person_eik: str | None = None
    obligated_person_name: str | None = None
    payment_paragraph: str | None = None
    period_from: str | None = None
    period_to: str | None = None
    amount_transferred: MoneyAmount = field(default_factory=lambda: MoneyAmount(None, None))
    all_nra_ibans: list[str] = field(default_factory=list)
    all_paragraphs: list[str] = field(default_factory=list)
    payment_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FiscalMemoryReportDetails:
    """Specialized model for fiscal memory reports (Z-reports, monthly reports under Наредба Н-18)."""
    device_number: str | None = None          # Индивидуален номер на фискалното устройство (e.g. DT514210 / 1189252)
    fiscal_memory_number: str | None = None   # Номер на фискалната памет (e.g. 79034158 / 50262438)
    report_scope: str = "PERIODIC"            # "DAILY" (дневен) or "PERIODIC" / "MONTHLY" (месечен/периодичен)
    period_from: str | None = None            # От дата (YYYY-MM-DD)
    period_to: str | None = None              # До дата (YYYY-MM-DD)
    last_document_number: str | None = None   # Номер на последен документ
    last_fiscal_document_number: str | None = None # Последен фискален документ
    klen_number: str | None = None            # КЛЕН номер
    turnover_total: MoneyAmount = field(default_factory=MoneyAmount) # Общ оборот
    vat_total: MoneyAmount = field(default_factory=MoneyAmount)      # Общо ДДС
    tax_base_total: MoneyAmount = field(default_factory=MoneyAmount) # Данъчна основа
    turnover_group_a: MoneyAmount = field(default_factory=MoneyAmount) # Оборот група А (0%)
    vat_group_a: MoneyAmount = field(default_factory=MoneyAmount)      # ДДС група А
    turnover_group_b: MoneyAmount = field(default_factory=MoneyAmount) # Оборот група Б (20%)
    vat_group_b: MoneyAmount = field(default_factory=MoneyAmount)      # ДДС група Б
    turnover_group_v: MoneyAmount = field(default_factory=MoneyAmount) # Оборот група В (20%/9%)
    vat_group_v: MoneyAmount = field(default_factory=MoneyAmount)      # ДДС група В
    turnover_group_g: MoneyAmount = field(default_factory=MoneyAmount) # Оборот група Г (9%/0%)
    vat_group_g: MoneyAmount = field(default_factory=MoneyAmount)      # ДДС група Г
    storno_turnover_total: MoneyAmount = field(default_factory=MoneyAmount) # Общ сторно оборот
    storno_vat_total: MoneyAmount = field(default_factory=MoneyAmount)      # Общо сторно ДДС


@dataclass
class GoodsReceiptDetails:
    """Specialized model for goods delivery and warehouse receipts (Стокови разписки)."""
    receipt_number: str | None = None
    receipt_date: str | None = None
    delivered_by: str | None = None   # Предал стоките
    received_by: str | None = None    # Получил стоките
    total_amount: MoneyAmount = field(default_factory=MoneyAmount)
    old_balance: MoneyAmount = field(default_factory=MoneyAmount)     # Старо салдо
    paid_today: MoneyAmount = field(default_factory=MoneyAmount)      # Платени за деня
    new_balance: MoneyAmount = field(default_factory=MoneyAmount)     # Ново салдо
    items_count: int = 0


@dataclass
class InvoiceMetadata:
    """Document header metadata."""
    invoice_number: str | None = None
    date_issued: str | None = None  # YYYY-MM-DD
    date_tax_event: str | None = None  # YYYY-MM-DD
    due_date: str | None = None  # YYYY-MM-DD
    place_issued: str | None = None
    ocr_confidence_score: float | None = None
    document_type: str = DocumentType.INVOICE.value
    original_invoice_number: str | None = None
    original_invoice_date: str | None = None
    correction_reason: str | None = None
    is_credit_note: bool = False
    is_debit_note: bool = False
    compiled_by: str | None = None   # Съставител / Издал (чл. 6, ал. 1, т. 5 от ЗСч)
    received_by: str | None = None   # Получил / Приел
    currency: str | None = None


@dataclass
class Invoice:
    """Top-level document model."""
    raw_ocr_evidence: dict[str, Any] | None = None
    invoice_metadata: InvoiceMetadata = field(default_factory=InvoiceMetadata)
    supplier: Party = field(default_factory=Party)
    recipient: Party = field(default_factory=Party)
    line_items: list[LineItem] = field(default_factory=list)
    financial_summary: FinancialSummary = field(default_factory=FinancialSummary)
    payment_details: PaymentDetails = field(default_factory=PaymentDetails)
    validation: ValidationResult = field(default_factory=ValidationResult)
    budget_payment: BudgetPaymentDetails | None = None
    fiscal_report: FiscalMemoryReportDetails | None = None
    goods_receipt: GoodsReceiptDetails | None = None
    legal_compliance_report: LegalComplianceReport | None = None


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------

class _InvoiceEncoder(json.JSONEncoder):
    """Custom encoder: Decimal → string, dataclass → dict, Enum → str."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            # Serialize as string to prevent floating-point corruption
            return str(obj)
        if isinstance(obj, Enum):
            return obj.value
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        return super().default(obj)


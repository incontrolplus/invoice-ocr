# Project: Bulgarian Invoice OCR & Document Understanding Pipeline

## Architecture
- **Pipeline Pattern**: Strict 3-Layer Document Understanding Pipeline
  1. **Layer 1: Raw OCR Evidence (`raw_ocr_evidence`)**: Ingests PDF / images, applies adaptive image preprocessing, runs multi-pass Tesseract OCR, tracks per-page spatial coordinates `[left, top, width, height]`, confidence scores, and flags `is_low_confidence = conf < 60`.
  2. **Layer 2: Normalized Document Data (`normalized_data`)**: Spatial layout clustering (lines/blocks), table reconstruction, deterministic Bulgarian field extraction (EIK Mod-11, IBAN Mod-97, VAT ID, monetary `Decimal` objects with explicit currency `{ "amount": ..., "currency": ... }`, raw amount-in-words text).
  3. **Layer 3: Validation Results (`validation_results`)**: Financial formulas verification (item sum == tax base, tax base * vat == vat amount, tax base + vat == total, dual currency parity), Bulgarian Euro transition rules (2026-01-01, 2026-08-08, dual currency, words cross-check), structured machine-readable issues (`code`, `message`, `severity`, `field`, `detected_value`, `expected_value`, `difference`).
- **CLI & I/O Architecture**:
  - Single-file mode: clean JSON to stdout; all logs and diagnostics to stderr.
  - Batch mode: `--input-dir`, `--output-dir` (default `results/`), `<stem>.json` per file, `batch_summary.json`.
  - Debug mode: `--debug`, `--debug-dir` (default `debug/`), rendered page PNGs, preprocessed images, token bounding box overlays, layout tree JSON.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Dependency Management | Install `pymupdf` and `pytest` in `.venv` | M1 | Survey 2 |
| 2 | PDF Multi-Page Rasterization | Rasterize single & multi-page PDFs using PyMuPDF at 300–400 DPI | M1 | Survey 1, 2, 3 |
| 3 | Image Multi-Format Ingestion | Load `.png`, `.jpg`, `.jpeg` files cleanly | M1 | Survey 1, 2 |
| 4 | Multi-Page Token Coordinate Tracking | Track `page_number` and bounding box `[left, top, width, height]` on all tokens | M1 | Survey 1, 2 |
| 5 | Strict Read-Only Source Protection | Never modify, delete, or write to `/Volumes/NO NAME/_ФАКТУРИ` | M1 | Survey 1, 2, 3 |
| 6 | OSD Orientation Correction | Detect orientation angle (0, 90, 180, 270) and rotate upright | M2 | Survey 1, 2 |
| 7 | Contour-Based Deskewing | Detect small skew angles (< 15°) and rotate image upright | M2 | Survey 1, 2 |
| 8 | Contrast Enhancement (CLAHE) | Apply CLAHE contrast enhancement for low-contrast / faded scans | M2 | Survey 1, 2 |
| 9 | Adaptive & Otsu Binarization | Generate clean binary image variants for OCR pass scoring | M2 | Survey 1, 2 |
| 10 | Multi-Pass Tesseract OCR Engine | Execute Tesseract with `lang="bul"` under PSM 3 and PSM 11 | M2 | Survey 1, 2 |
| 11 | OCR Pass Scoring Engine | Select optimal OCR pass by character count, confidence, and Cyrillic density | M2 | Survey 1, 2 |
| 12 | Low-Confidence Token Tagging | Tag tokens with `conf < 60` as low-confidence evidence without discarding | M2 | Survey 1, 2 |
| 13 | Coordinate Line Grouping | Group tokens geometrically into logical lines by spatial overlap & proximity | M3 | Survey 1, 2 |
| 14 | Spatial Block Grouping | Group logical lines into spatial blocks by vertical gaps and alignment | M3 | Survey 1, 2 |
| 15 | Table Header Recognition | Identify table columns by Bulgarian synonyms (№, описание, количество, мярка, цена, стойност, ДДС) | M3 | Survey 1, 2, 3 |
| 16 | Multi-Line Description Aggregation | Cluster wrapped multi-line descriptions into single line item rows | M3 | Survey 1, 2, 3 |
| 17 | Multi-Page Table Continuation | Reconstruct tables continuing across consecutive pages | M3 | Survey 1, 2, 3 |
| 18 | No Synthetic Fallback Descriptions | Assign `null` when description unresolvable with warning; never use synthetic `"Item"` | M3 | Survey 1, 3 |
| 19 | Document Occlusion & Receipt Isolation | Handle physical slip occlusion (капина-03), isolate receipt lines from invoice table | M3 | Survey 1, 3 |
| 20 | Invoice Number Extraction | Extract 10-digit Bulgarian statutory invoice number (padded with zeroes) | M4 | Survey 1, 2, 3 |
| 21 | Date Issued & Tax Event Date | Extract issue date and tax event date in Bulgarian formats (`DD.MM.YYYY`) to ISO | M4 | Survey 1, 2, 3 |
| 22 | Place of Issue Extraction | Extract settlement of issue (e.g. `гр. Плевен`, `гр. София`) | M4 | Survey 1, 2, 3 |
| 23 | Supplier & Recipient Zoning | Classify parties by semantic keywords and 2-column horizontal bounding box zones | M4 | Survey 1, 2, 3 |
| 24 | Bulgarian 9-Digit EIK Checksum | Validate 9-digit EIK via two-pass Modulo-11 algorithm (weights 1..8 and 3..10) | M4 | Survey 1, 2 |
| 25 | Bulgarian 13-Digit EIK Checksum | Validate 13-digit branch EIK via Modulo-11 algorithm (weights 2,7,3,5 & 4,9,5,7) | M4 | Survey 1, 2 |
| 26 | VAT ID Normalization | Normalize VAT ID to `BG` + valid digits (9/10/13) | M4 | Survey 1, 2 |
| 27 | Bulgarian IBAN Modulo-97 Checksum | Validate 22-char Bulgarian IBAN structure (`BG`+2+4+4+2+8) and ISO 7064 Mod-97 | M4 | Survey 1, 2 |
| 28 | BIC/SWIFT Extraction | Extract and regex-validate 8 or 11 character BIC code | M4 | Survey 1, 2 |
| 29 | Monetary Decimal Parsing | Parse European/Bulgarian decimal formats into Python `Decimal` objects | M4 | Survey 1, 2 |
| 30 | Explicit Currency Objects Schema | Wrap all monetary values in `{ "amount": ..., "currency": ... }` | M4 | Survey 1, 2, 3 |
| 31 | Verbatim Amount-in-Words Extraction | Capture raw textual evidence of amount in words without modification | M4 | Survey 1, 2, 3 |
| 32 | Line Items Sum Validation | Verify `sum(line_items.total_price_net) == tax_base` within 0.02 tolerance | M5 | Survey 1, 2, 3 |
| 33 | Statutory VAT Rate & Amount Validation | Verify `tax_base * vat_rate == vat_amount` against statutory rates (20%, 9%, 0%) (tolerance 0.02) | M5 | Survey 1, 2 |
| 34 | Total Due Sum Validation | Verify `tax_base + vat_amount == total_amount_due` within 0.02 tolerance | M5 | Survey 1, 2, 3 |
| 35 | Dual Currency Parity Verification | Verify `abs(total_bgn - total_eur * Decimal("1.95583")) <= 0.02` | M5 | Survey 1, 3 |
| 36 | Euro Transition Date >= 2026-01-01 | Flag BGN primary currency on invoices dated >= `2026-01-01`; strictly NO auto-convert | M5 | Survey 1, 2 |
| 37 | Euro Transition Date >= 2026-08-08 | Flag BGN as primary payable currency after dual-display deadline (`2026-08-08`) | M5 | Survey 1, 2 |
| 38 | Dual Currency Informational Warning | Flag `DUAL_CURRENCY_DETECTED` when both BGN and EUR appear | M5 | Survey 1, 3 |
| 39 | Amount Words vs Numeric Currency Check | Cross-check amount-in-words currency against numeric currency without modifying numbers | M5 | Survey 1, 3 |
| 40 | Strict 3-Layer Output Serialization | Emit JSON with top-level `raw_ocr_evidence`, `normalized_data`, and `validation_results` | M5 | Survey 1, 2 |
| 41 | CLI Single-File Mode (Stdout JSON / Stderr Logs) | Output strictly valid JSON to stdout; all logging, progress, diagnostics to stderr | M6 | Survey 1, 2 |
| 42 | CLI Batch Processing Mode | Walk `--input-dir`, process independently, output JSONs to `--output-dir` | M6 | Survey 1, 2 |
| 43 | Batch Summary Generation | Generate `batch_summary.json` with aggregate processing counts and metrics | M6 | Survey 1, 2 |
| 44 | Debug Mode & Visual Artifacts Export | Save rendered pages, preprocessed images, token bounding boxes, layout tree to `--debug-dir` | M6 | Survey 1, 2 |
| 45 | Comprehensive E2E Testing Suite | Multi-tier test suite (Tiers 1-4) derived from user specifications | M_E2E | Survey 1, 2, 3 |
| 46 | Acceptance Dataset Validation & Audit Table | 100% pass on Kapina 01, 02, 03; zero source file modifications; diagnostic audit table | M7 | Survey 1, 2, 3 |
| 47 | Adversarial Coverage Hardening | White-box stress testing and edge-case coverage audit | M7 | Survey 1, 2 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M_E2E | E2E Testing Track | Requirement-driven test suite (Tiers 1-4) & test runner; publishes `TEST_READY.md` | none | DONE |
| M1 | Multi-Format Ingestion | Install `pymupdf` & `pytest` in `.venv`; PDF rasterization (300 DPI); image ingestion; token `page_number` & bbox tracking; read-only dataset protection (Features 1-5) | none | DONE |
| M2 | Adaptive Preprocessing & Multi-Pass OCR | OSD orientation; deskewing; CLAHE contrast; Otsu/adaptive binarization; multi-pass Tesseract (`bul`) PSM 3 & 11; scoring; low-confidence tagging (Features 6-12) | M1 | DONE |
| M3 | Spatial Layout & Table Reconstruction | Coordinate line & block grouping; Bulgarian column synonym matching; multi-line description merging; multi-page tables; occlusion handling & null fallbacks (Features 13-19) | M2 | PLANNED |
| M4 | Deterministic Extraction & Tax Rules | Invoice metadata; spatial party zoning; 9/13 EIK Mod-11; 22-char IBAN Mod-97; VAT ID; Decimal money with explicit currency; raw amount in words (Features 20-31) | M3 | PLANNED |
| M5 | Financial Validation & Euro Engine | Financial formulas (items sum, VAT calculation, total sum, dual currency parity); Euro transition 2026 rules; 3-layer architecture schema (Features 32-40) | M4 | PLANNED |
| M6 | CLI, Batch & Debug Artifacts | Single-file mode (stdout JSON / stderr logs); batch mode (`--input-dir`, `--output-dir`, `batch_summary.json`); debug mode (`--debug`, `--debug-dir`) (Features 41-44) | M5 | PLANNED |
| M7 | Final Acceptance & Adversarial Hardening | Phase 1: 100% pass on E2E test suite (Tiers 1-4), 3 Kapina acceptance files, zero-touch verification, diagnostic audit table. Phase 2: Adversarial coverage hardening (Tier 5) (Features 46-47) | M6, M_E2E | PLANNED |

## Interface Contracts

### Ingestion ↔ Preprocessing & OCR (`invoice_ocr.py`)
```python
@dataclass
class OcrToken:
    text: str
    conf: float
    bbox: tuple[int, int, int, int]  # (left, top, width, height)
    page_number: int
    is_low_confidence: bool  # conf < 60

@dataclass
class PageImage:
    page_number: int
    image: np.ndarray  # BGR image
    width: int
    height: int
```

### Preprocessing & OCR ↔ Layout & Table Reconstruction
```python
@dataclass
class LogicalLine:
    tokens: list[OcrToken]
    bbox: tuple[int, int, int, int]
    text: str
    page_number: int
    y_center: float

@dataclass
class TableRegion:
    columns: list[TableColumn]
    header_line: LogicalLine
    data_lines: list[LogicalLine]
    page_number: int
```

### Layout & Table ↔ Field Extraction & Data Models
```python
@dataclass
class MoneyAmount:
    amount: Decimal | None
    currency: str | None  # "BGN", "EUR", etc.

@dataclass
class LineItem:
    index: int
    description: str | None
    unit: str | None
    quantity: Decimal | None
    unit_price_net: MoneyAmount
    total_price_net: MoneyAmount
    vat_rate_pct: Decimal | None
```

### Extraction ↔ Financial Validation & Output Serialization
```python
@dataclass
class ValidationIssue:
    code: str
    message: str
    severity: str  # "error" | "warning"
    field: str | None = None
    detected_value: str | None = None
    expected_value: str | None = None
    difference: str | None = None

@dataclass
class ValidationSummary:
    is_valid: bool
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]
```

### Strict 3-Layer Output Contract
```json
{
  "raw_ocr_evidence": {
    "total_pages": int,
    "pages": [{ "page_number": int, "width": int, "height": int, "tokens": [...] }],
    "total_tokens": int,
    "mean_confidence": float,
    "low_confidence_count": int
  },
  "normalized_data": {
    "invoice_metadata": { ... },
    "supplier": { ... },
    "recipient": { ... },
    "line_items": [ ... ],
    "financial_summary": { ... },
    "payment_details": { ... }
  },
  "validation_results": {
    "is_valid": bool,
    "errors": [ ... ],
    "warnings": [ ... ]
  }
}
```

## Code Layout
- Main Engine: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py`
- Test Suites:
  - Unit Tests: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_invoice_ocr.py`
  - E2E Test Runner & Suite: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/e2e/`
- Output Directories:
  - Batch Results: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/results/`
  - Debug Artifacts: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/debug/`
- Agent Metadata: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/`

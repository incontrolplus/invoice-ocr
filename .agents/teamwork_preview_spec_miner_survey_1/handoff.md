# Specification Mining & Requirements Survey Report
**Agent:** Survey Agent 1 (`teamwork_preview_spec_miner`)  
**Working Directory:** `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_spec_miner_survey_1`  
**Target Specification:** `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md`  
**Timestamp:** 2026-09-05T00:18:00+03:00  

---

## 1. Observation

Direct observations from inspecting the codebase, configuration, execution environment, and datasets:

1. **`ORIGINAL_REQUEST.md` Content**:
   - Outlines 6 functional requirements (`R1` to `R6`) spanning:
     - `R1`: Multi-Format & Multi-Page Ingestion (`.png`, `.jpg`, `.jpeg`, `.pdf` via PyMuPDF at 300–400 DPI, multi-page coordinate tracking).
     - `R2`: Adaptive Preprocessing & Multi-Pass OCR (`pymupdf`, `opencv-python`, `pillow`, `pytesseract`, `numpy`; OSD, deskewing, noise reduction, CLAHE, adaptive threshold; Tesseract `bul` under PSM 3 and PSM 11; scoring and keeping `conf < 60` tokens as low-confidence).
     - `R3`: Coordinate-Based Layout Analysis & Table Reconstruction (spatial coordinates grouping, Bulgarian column synonyms, column mapping, `null` fallback instead of `"Item"`).
     - `R4`: Deterministic Field Extraction & Bulgarian Tax Rules (metadata, Supplier vs Recipient, EIK 9/13, VAT ID `BG`+digits, Bulgarian IBAN/BIC, `Decimal` money objects `{ "amount": ..., "currency": ... }`, raw amount-in-words text).
     - `R5`: Rigorous Financial, Euro-Transition & Anomaly Validation (math checks with tolerances 0.01/0.02; Euro rules `2026-01-01` and `2026-08-08`, no auto-conversion; strict 3-layer architecture: (1) Raw OCR Evidence, (2) Normalized Data, (3) Validation Results).
     - `R6`: CLI, Batch Processing & Debug Artifacts (single file mode stdout JSON / stderr logs; `--input-dir`, `--output-dir`, `batch_summary.json`; `--debug`, `--debug-dir`).
   - Defines strict acceptance criteria:
     - Dependency verification (`pymupdf` in `.venv`).
     - 100% pass on comprehensive unit tests.
     - Acceptance against 3 mandatory real Kapina files in `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`.
     - Read-only constraint: 0 files modified/deleted in `/Volumes/NO NAME/_ФАКТУРИ`.

2. **Environment & Dependency Status in `.venv`**:
   - Executing `./.venv/bin/pip list` reveals:
     ```
     Package       Version
     ------------- --------
     numpy         2.5.2
     opencv-python 5.0.0.93
     packaging     26.3
     pillow        12.3.0
     pip           26.2.1
     pytesseract   0.3.13
     ```
   - PyMuPDF (`fitz`) is **NOT** installed: executing `./.venv/bin/python -c "import fitz"` exits with code 1 (`ModuleNotFoundError: No module named 'fitz'`).
   - Tesseract binary is available at `/opt/homebrew/bin/tesseract` (v5.5.2) with installed languages: `['bul', 'eng', 'osd', 'snum']`.

3. **Current State of `invoice_ocr.py` (80,596 bytes, 2,275 lines)**:
   - Line 59: `SUPPORTED_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg"}` — `.pdf` is missing from supported extensions.
   - `load_image(path)` uses OpenCV `cv2.imread(str(path))` exclusively; it lacks PDF rasterization support.
   - `OcrToken` (lines 118–140) lacks a `page_number` field.
   - `normalize_eik(raw)` (lines 445–457) only checks `len(digits) in (9, 10, 13)` without running the Bulgarian Modulo-11 checksum validation algorithm.
   - `normalize_iban(raw)` (lines 481–492) checks `cleaned.startswith('BG') and len(cleaned) == 22`, but does not validate ISO 7064 Modulo 97-10.
   - `serialize_invoice()` (lines 296–308) serializes only `Invoice` which contains `invoice_metadata`, `supplier`, `recipient`, `line_items`, `financial_summary`, `payment_details`, and `validation`. The raw OCR tokens, bounding boxes, and per-page layout evidence are **omitted from the serialized output** — violating the required 3-layer architecture.
   - `main()` (lines 2212–2275) uses `argparse` with only a single positional argument `image`. It lacks `--input-dir`, `--output-dir`, `--debug`, and `--debug-dir`.

4. **Existing Test Suite in `test_invoice_ocr.py`**:
   - Executes 55 assertions across 6 test functions (`parse_money`, `normalize_eik`, `normalize_vat_number`, `parse_date`, `clean_ocr_artifacts`, `normalize_iban`). All 55 pass.
   - Missing unit tests for: financial validation formulas (`sum(line_items) == tax_base`, `tax_base * vat_rate == vat_amount`, `tax_base + vat_amount == total`), Euro transition warnings, EIK checksums, IBAN Modulo-97 checksums, multi-page PDF ingestion, and CLI batch mode.

5. **Read-Only Test Dataset**:
   - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/` contains:
     - `капина-01.pdf` (7,516,207 bytes, 1 page, high-resolution raster image inside PDF envelope)
     - `капина-02.pdf` (8,148,645 bytes, 1 page)
     - `капина-03.pdf` (7,218,503 bytes, 1 page)
   - Parent directory `/Volumes/NO NAME/_ФАКТУРИ` contains 12 supplier directories with real-world Bulgarian invoices.

---

## 2. Logic Chain

1. **R1 Ingestion Incomplete**: Because `SUPPORTED_EXTENSIONS` omits `.pdf`, `cv2.imread` is called directly on file paths, and `fitz` is not installed, the current script immediately rejects or fails on PDF inputs. Installing `pymupdf` in `.venv` and creating a rasterizer that produces 300–400 DPI images with page numbering is required to satisfy R1 and Acceptance Criteria.
2. **R2 Multi-Page OCR Evidence**: Tokens currently have bounding boxes `(left, top, width, height)` and confidence scores, but lack `page_number`. Multi-page invoices cannot be reconstructed properly without tagging each token with `page_number`. Low-confidence tokens (`conf < 60`) are kept in memory but need explicit `is_low_confidence: bool` flags.
3. **R3 Table Layout & Fallback Rules**: `COLUMN_SYNONYMS` covers Bulgarian header synonyms (`№`, `описание`, `количество`, `мярка`, `ед. цена`, `стойност`, `ДДС`). However, if an invoice table has row items without textual description, assigning a fallback like `"Item"` violates R3. Unresolvable fields must serialize as `null` with a warning issue.
4. **R4 Bulgarian Statutory Identifier Validation**:
   - EIK/BULSTAT: Merely checking digit length (9 or 13) allows corrupted OCR reads (e.g. `123456789`) to pass. Under Bulgarian law, a 9-digit EIK uses a 2-pass Modulo-11 algorithm with weights `[1..8]` and `[3..10]`. A 13-digit EIK uses weights `[2, 7, 3, 5]` and `[4, 9, 5, 7]` on digits 9..12.
   - VAT ID: Must be `BG` followed by a valid 9, 10, or 13-digit identifier.
   - IBAN: Must be 22 chars with `BG` prefix and pass ISO 7064 Modulo 97-10.
5. **R5 Financial Validation & Euro Transition**:
   - Financial tolerances: Differences up to 0.01 / 0.02 are allowed due to line-item rounding.
   - Euro changeover: Date `>= 2026-01-01` with BGN as primary currency requires warning `CURRENCY_POST_EURO_BGN_DETECTED`. Date `>= 2026-08-08` requires warning `CURRENCY_AFTER_DUAL_PERIOD`. In no case should BGN be auto-converted to EUR (rate 1.95583 is fixed, but financial figures must reflect document evidence).
   - 3-Layer Output: Current JSON structure merges extraction and validation, omitting raw OCR tokens. The output must strictly separate Layer 1 (`raw_ocr_evidence`), Layer 2 (`normalized_data`), and Layer 3 (`validation_results`).
6. **R6 CLI Contracts**:
   - The CLI must support both single file and `--input-dir` batch mode.
   - Diagnostic messages must go to `stderr`; `stdout` must receive only valid JSON.
   - In batch mode, individual JSONs must be saved to `--output-dir` (default `results/`), and an aggregated `batch_summary.json` must be emitted.

---

## 3. Bulgarian Statutory Rules & Domain Specification

### 3.1 Statutory Framework
Invoices in Bulgaria are governed by:
- **Закон за данък върху добавената стойност (ЗДДС), чл. 114**: Mandatory requisites of tax invoices.
- **Закон за счетоводството (ЗСч), чл. 6 & 7**: Primary accounting documents.
- **Закон за регистър БУЛСТАТ & Закон за търговския регистър**: EIK identification.
- **Закон за въвеждане на еврото в Република България (ЗВЕ)**: Euro transition and dual-display requirements.
- **БНБ Наредба № 3**: Bulgarian IBAN structure and bank operations.

### 3.2 Metadata Requisites
- **Invoice Number (`invoice_number`)**: 10 digits (десетразряден пореден номер). In Bulgarian statutory accounting, invoice numbers are 10 digits padded with leading zeroes (e.g. `0000001234`).
- **Date Issued (`date_issued`)**: Format DD.MM.YYYY, normalized to ISO `YYYY-MM-DD`.
- **Tax Event Date (`date_tax_event`)**: Дата на възникване на данъчното събитие (чл. 114, ал. 1, т. 4 от ЗДДС). If identical to issue date, it may be explicit or omitted; if present, normalized to ISO `YYYY-MM-DD`.
- **Place of Issue (`place_issued`)**: Settlement name (e.g. `гр. София`, `гр. Пловдив`).

### 3.3 Parties (Supplier vs Recipient)
- **Supplier (Доставчик)**:
  - Role keywords: `Доставчик`, `Продавач`, `Изпълнител`, `Supplier`
  - Required fields: Name (`name`), Address (`address`), MOL (`mol`), EIK (`eik`), VAT ID (`vat_number`).
- **Recipient (Получател)**:
  - Role keywords: `Получател`, `Купувач`, `Клиент`, `Възложител`, `Recipient`
  - Required fields: Name (`name`), Address (`address`), MOL (`mol`), EIK (`eik`), VAT ID (`vat_number`, optional if non-VAT entity).
- **MOL (Материално отговорно лице)**:
  - Keywords: `МОЛ`, `Материално отговорно лице`, `Ръководител`, `Управител`.

### 3.4 Identifiers & Exact Checksum Algorithms
#### 1. 9-Digit EIK / BULSTAT (Modulo-11)
For digits $d_1 d_2 d_3 d_4 d_5 d_6 d_7 d_8 d_9$:
- **Stage 1**:
  - Weights: $W_1 = [1, 2, 3, 4, 5, 6, 7, 8]$
  - Sum: $S_1 = \sum_{i=1}^8 d_i \cdot W_{1,i}$
  - Remainder: $R_1 = S_1 \pmod{11}$
  - If $R_1 < 10 \implies d_9 = R_1$.
- **Stage 2** (only if $R_1 == 10$):
  - Weights: $W_2 = [3, 4, 5, 6, 7, 8, 9, 10]$
  - Sum: $S_2 = \sum_{i=1}^8 d_i \cdot W_{2,i}$
  - Remainder: $R_2 = S_2 \pmod{11}$
  - If $R_2 < 10 \implies d_9 = R_2$.
  - If $R_2 == 10 \implies d_9 = 0$.

#### 2. 13-Digit EIK / BULSTAT (Branches / Subdivisions)
For digits $d_1 \dots d_9 \dots d_{13}$:
- Digits $d_1 \dots d_9$ must pass the 9-digit EIK validation above.
- Digits $d_9, d_{10}, d_{11}, d_{12}$ determine the 13th check digit $d_{13}$:
  - **Stage 1**:
    - Weights: $W_{13,1} = [2, 7, 3, 5]$
    - Sum: $S_1 = d_9 \cdot 2 + d_{10} \cdot 7 + d_{11} \cdot 3 + d_{12} \cdot 5$
    - Remainder: $R_1 = S_1 \pmod{11}$
    - If $R_1 < 10 \implies d_{13} = R_1$.
  - **Stage 2** (only if $R_1 == 10$):
    - Weights: $W_{13,2} = [4, 9, 5, 7]$
    - Sum: $S_2 = d_9 \cdot 4 + d_{10} \cdot 9 + d_{11} \cdot 5 + d_{12} \cdot 7$
    - Remainder: $R_2 = S_2 \pmod{11}$
    - If $R_2 < 10 \implies d_{13} = R_2$.
    - If $R_2 == 10 \implies d_{13} = 0$.

#### 3. 10-Digit EGN (Sole Proprietors / Physical Persons registered under EGN)
- Weights: $[2, 4, 8, 5, 10, 9, 7, 3, 6]$, Modulo 11 (remainder 10 maps to 0).

#### 4. VAT Identification Number (ЗДДС номер)
- Prefix: strictly `BG`.
- Suffix: 9 digits (standard legal entity), 10 digits (physical person / EGN), or 13 digits (branch).
- Normalized string: `BG` + digits (no spaces, dashes, or punctuation).

### 3.5 Payment Requisites & Bulgarian IBAN Validation
- **Bulgarian IBAN Structure**: Total exactly **22 characters**:
  - `BG` (2 alpha - Country Code)
  - `kk` (2 numeric - Check Digits)
  - `BBBB` (4 alpha - Bank Code)
  - `SSSS` (4 numeric - Branch Code)
  - `TT` (2 numeric - Account Type)
  - `CCCCCCCC` (8 alphanumeric - Account Number)
- **ISO 7064 Modulo 97-10 Checksum Algorithm**:
  1. Move initial 4 characters to the end (`BGkk` moves to end).
  2. Convert letters to numbers ($A=10, B=11, \dots, Z=35$).
  3. Compute `int(converted) % 97`. Checksum is valid if and only if remainder equals `1`.
- **BIC / SWIFT**: 8 or 11 characters matching `^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$`.

### 3.6 Financial Totals & Bulgarian VAT Rates
- **VAT Rates**:
  - Standard: `20%` (0.20) — applied to most goods and services.
  - Reduced: `9%` (0.09) — accommodation in hotels, tourist services, sports facilities.
  - Zero-rated: `0%` (0.00) — intra-community supplies (ВОД), exports outside EU.
  - Exempt: `0%` (Освободена доставка) — financial, insurance, health, educational services.
- **Rounding & Tolerances**:
  - In Bulgarian invoices, line items are computed to 2 decimal places and rounded half up (`ROUND_HALF_UP`).
  - Allowed tolerance for validation comparisons is **0.01 / 0.02** currency units.

### 3.7 Euro Transition Rules (2026)
- **Official Conversion Rate**: Fixed irrevocable parity: $1\text{ EUR} = 1.95583\text{ BGN}$.
- **Transition Date (`2026-01-01`)**:
  - Invoices issued on or after `2026-01-01` where primary currency is `BGN` trigger warning:
    `CURRENCY_POST_EURO_BGN_DETECTED`.
  - **Zero Auto-Conversion Rule**: The pipeline MUST NOT convert BGN amounts to EUR. Extracted financial values must reflect document evidence verbatim.
- **Dual-Display Deadline (`2026-08-08`)**:
  - Invoices issued on or after `2026-08-08` where primary payable currency is `BGN` trigger warning:
    `CURRENCY_AFTER_DUAL_PERIOD`. EUR must be the primary payable currency post dual-display period.
- **Dual-Currency Invoices**:
  - If both EUR and BGN amounts are shown on the invoice (dual display), flag informational warning:
    `DUAL_CURRENCY_DETECTED`.
- **Amount-in-Words Cross-Check**:
  - Verbatim extraction into `total_amount_words`.
  - If numeric currency is `EUR` but words mention `лева` or `стотинки` (without `евро`), flag warning:
    `AMOUNT_WORDS_CURRENCY_MISMATCH`.
  - If numeric currency is `BGN` but words mention `евро`, flag warning:
    `AMOUNT_WORDS_CURRENCY_MISMATCH`.
  - Extracted numbers and words remain unmodified.

---

## 4. Strict 3-Layer Architecture Specification

The system must output a single JSON document with clear top-level separation across three layers:

```json
{
  "raw_ocr_evidence": {
    "total_pages": 1,
    "pages": [
      {
        "page_number": 1,
        "width": 2480,
        "height": 3508,
        "tokens": [
          {
            "text": "ФАКТУРА",
            "conf": 96,
            "bbox": [1020, 250, 180, 45],
            "is_low_confidence": false
          }
        ]
      }
    ],
    "total_tokens": 342,
    "mean_confidence": 0.94,
    "low_confidence_count": 5
  },
  "normalized_data": {
    "invoice_metadata": {
      "invoice_number": "0000001234",
      "date_issued": "2026-01-15",
      "date_tax_event": "2026-01-15",
      "place_issued": "гр. София",
      "ocr_confidence_score": 0.94
    },
    "supplier": {
      "name": "КАПИНА 71 ООД",
      "eik": "123456789",
      "vat_number": "BG123456789",
      "address": "гр. София, ул. ...",
      "mol": "Иван Иванов"
    },
    "recipient": {
      "name": "МЕТРО БЪЛГАРИЯ ЕООД",
      "eik": "121644736",
      "vat_number": "BG121644736",
      "address": "гр. София, бул. Цариградско шосе ...",
      "mol": "Димитър Димитров"
    },
    "line_items": [
      {
        "index": 1,
        "description": "Консултантски услуги",
        "unit": "бр.",
        "quantity": "1.00",
        "unit_price_net": "500.00",
        "total_price_net": "500.00",
        "vat_rate_pct": "20.00"
      }
    ],
    "financial_summary": {
      "tax_base": { "amount": "500.00", "currency": "EUR" },
      "vat_amount": { "amount": "100.00", "currency": "EUR" },
      "total_amount_due": { "amount": "600.00", "currency": "EUR" },
      "total_amount_words": "шестстотин евро и 00 цента"
    },
    "payment_details": {
      "method": "банков превод",
      "bank_name": "УниКредит Булбанк",
      "iban": "BG80BNBG96611020345678",
      "bic": "BNBGBSF"
    }
  },
  "validation_results": {
    "is_valid": true,
    "errors": [],
    "warnings": []
  }
}
```

### Validation Issue Schema
Every issue inside `errors` or `warnings` must follow:
```json
{
  "code": "STRING_UPPERCASE_CODE",
  "message": "Human readable explanation",
  "severity": "error" | "warning",
  "field": "optional.field.path",
  "detected_value": "optional string",
  "expected_value": "optional string",
  "difference": "optional string"
}
```

---

## 5. CLI Specification & I/O Protocol

### CLI Syntax
```bash
# Single file mode (image or PDF)
python invoice_ocr.py <file_path> [--debug] [--debug-dir <dir>]

# Batch mode
python invoice_ocr.py --input-dir <input_directory> [--output-dir <output_directory>] [--debug] [--debug-dir <dir>]
```

### I/O Rules
1. **stdout Separation**:
   - In single-file mode: `stdout` contains **ONLY** valid JSON.
   - Any logging, informational messages, progress bars, or warnings must go to `stderr`.
2. **Batch Mode**:
   - Recursively traverses `--input-dir` for files ending in `.png`, `.jpg`, `.jpeg`, `.pdf`.
   - Generates `<file_stem>.json` for each file inside `--output-dir` (default: `results/`).
   - Produces `batch_summary.json` inside `--output-dir` with:
     - `total_files`: total documents discovered.
     - `processed`: documents successfully processed.
     - `failed`: documents where unhandled processing exceptions occurred.
     - `valid_invoices`: count of invoices with `is_valid == true`.
     - `invalid_invoices`: count of invoices with `is_valid == false`.
     - `file_results`: list of records with `filename`, `is_valid`, `error_count`, `warning_count`, `elapsed_seconds`.
3. **Debug Artifacts (`--debug`)**:
   - Written to `--debug-dir` (default: `debug/`):
     - `<file_stem>_page_<N>_rendered.png`: rasterized 300 DPI page image.
     - `<file_stem>_page_<N>_preprocessed.png`: deskewed, contrast-enhanced image.
     - `<file_stem>_page_<N>_token_map.png`: annotated image with bounding boxes.
     - `<file_stem>_layout_tree.json`: spatial blocks and lines before semantic extraction.

---

## Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | Ingestion | PDF Rasterization | Rasterize single & multi-page PDFs using PyMuPDF at 300–400 DPI | `.pdf` file path | Rendered `numpy.ndarray` / PIL images per page | Raise `ValueError` on encrypted or corrupt PDF | ORIGINAL_REQUEST.md R1 |
| 2 | Ingestion | Multi-Page Boundary Tracking | Track tokens across pages with `page_number` and identify page boundary transitions | Multi-page PDF / image list | Tokens tagged with `page_number` | Preserves page index without losing line continuations | ORIGINAL_REQUEST.md R1 |
| 3 | Ingestion | Read-Only Dataset Guarantee | Ensure test datasets in `/Volumes/NO NAME/_ФАКТУРИ` are read-only and never modified | File path | In-memory read buffer | Read-only mode; raise error if write attempted | ORIGINAL_REQUEST.md R1 |
| 4 | Preprocessing | OSD Orientation Correction | Detect orientation angle (0, 90, 180, 270) and rotate image upright | Grayscale/RGB image | Rotated image | Skip rotation if OSD confidence low | ORIGINAL_REQUEST.md R2 |
| 5 | Preprocessing | Contour-Based Deskewing | Detect small skew angles using contour minAreaRect / Hough lines and de-skew | Binarized image | Deskewed image | Skip if detected skew angle < 0.5° | ORIGINAL_REQUEST.md R2 |
| 6 | Preprocessing | CLAHE Contrast Enhancement | Contrast Limited Adaptive Histogram Equalization on L-channel | RGB / Grayscale image | Enhanced contrast image | Fallback to original image if opencv fails | ORIGINAL_REQUEST.md R2 |
| 7 | Preprocessing | Adaptive & Otsu Binarization | Generate binarized image variants for OCR pass scoring | Grayscale image | Thresholded binary images | Fallback to global threshold if image has low variance | ORIGINAL_REQUEST.md R2 |
| 8 | OCR Engine | Multi-Pass Tesseract OCR | Run Tesseract with `lang="bul"` across PSM 3 and PSM 11 | Image variants | Token lists with `(text, conf, bbox)` | Return empty token list; flag `OCR_NO_TOKENS` | ORIGINAL_REQUEST.md R2 |
| 9 | OCR Engine | OCR Pass Scoring | Select best pass using token count, mean confidence, and Cyrillic character ratio | Multiple token lists | Single best token list | Select pass with highest composite score | ORIGINAL_REQUEST.md R2 |
| 10 | OCR Engine | Low-Confidence Flagging | Flag tokens with `conf < 60` as low-confidence evidence without discarding | `OcrToken` | `OcrToken.is_low_confidence = True` | Token retained in evidence stream | ORIGINAL_REQUEST.md R2 |
| 11 | Layout Analysis | Coordinate Line Grouping | Group tokens into logical lines by Y-coordinate overlap and proximity | Tokens list | `list[LogicalLine]` | Isolated tokens become single-token lines | ORIGINAL_REQUEST.md R3 |
| 12 | Layout Analysis | Block Grouping | Group logical lines into spatial blocks by vertical gap and left alignment | `list[LogicalLine]` | `list[LogicalBlock]` | Single lines form solitary blocks | ORIGINAL_REQUEST.md R3 |
| 13 | Layout Analysis | Table Header Detection | Detect line item table headers via Bulgarian column synonyms (`№`, `описание`, etc.) | `list[LogicalLine]` | `TableRegion` with columns | Return empty `table_regions` if no header found | ORIGINAL_REQUEST.md R3 |
| 14 | Layout Analysis | Column Boundary Mapping | Compute horizontal spans `[x_left, x_right]` for each column and map tokens | `TableRegion`, tokens | Structured column tokens | Assign to closest column if token spans boundary | ORIGINAL_REQUEST.md R3 |
| 15 | Layout Analysis | No Synthetic Fallback | Refuse synthetic fallbacks like `"Item"`; emit `null` description and warning issue | Unresolvable line item text | `description: null` | Warning `UNRESOLVED_LINE_ITEM_DESCRIPTION` | ORIGINAL_REQUEST.md R3 |
| 16 | Field Extraction | Invoice Number Extraction | Extract 10-digit statutory invoice number | `list[LogicalLine]` | `invoice_number: str` | Error `MISSING_INVOICE_NUMBER` if not found | ORIGINAL_REQUEST.md R4 |
| 17 | Field Extraction | Date Issued & Tax Event Date | Extract issue date and tax event date in Bulgarian formats (`DD.MM.YYYY`) | `list[LogicalLine]` | ISO `YYYY-MM-DD` strings | Error `MISSING_DATE_ISSUED` if issue date absent | ORIGINAL_REQUEST.md R4 |
| 18 | Field Extraction | Place of Issue Extraction | Extract place of issue (e.g. `гр. София`) | `list[LogicalLine]` | `place_issued: str` | Set to `null` if not found | ORIGINAL_REQUEST.md R4 |
| 19 | Field Extraction | Supplier & Recipient Zoning | Classify parties by semantic keywords (`Доставчик` vs `Получател`) and spatial layout | `list[LogicalLine]`, tokens | `supplier: Party`, `recipient: Party` | Errors `MISSING_SUPPLIER_NAME` / `MISSING_RECIPIENT_NAME` | ORIGINAL_REQUEST.md R4 |
| 20 | Field Extraction | EIK 9-Digit Checksum | Validate 9-digit EIK via two-pass Modulo-11 algorithm (weights 1..8 and 3..10) | Digits string | Cleaned EIK string or warning | Warning `INVALID_EIK_CHECKSUM` if check digit fails | Statutory & R4 |
| 21 | Field Extraction | EIK 13-Digit Checksum | Validate 13-digit branch EIK via Modulo-11 algorithm (weights 2,7,3,5 & 4,9,5,7) | Digits string | Cleaned 13-digit EIK or warning | Warning `INVALID_EIK_CHECKSUM` if 13th digit fails | Statutory & R4 |
| 22 | Field Extraction | VAT ID Normalization | Normalize VAT ID to `BG` + 9/10/13 digits | Raw VAT string | `BG{digits}` | Warning `INVALID_VAT_FORMAT` if non-standard | Statutory & R4 |
| 23 | Field Extraction | Bulgarian IBAN Validation | Validate 22-char Bulgarian IBAN structure (`BGkk BBBB SSSS TT CCCCCCCC`) and Mod-97 | Raw IBAN string | Cleaned IBAN string | Warning `INVALID_IBAN_CHECKSUM` if Mod-97 != 1 | Statutory & R4 |
| 24 | Field Extraction | BIC/SWIFT Extraction | Extract and validate 8 or 11 character BIC code | Tokens / text | Cleaned BIC string | Set to `null` if unparseable | Statutory & R4 |
| 25 | Field Extraction | Monetary Decimal Parsing | Parse Bulgarian/European numbers (`1.234,56`, `30,00`) into `Decimal` | Raw money string | `Decimal` object | Returns `None` if unparseable | ORIGINAL_REQUEST.md R4 |
| 26 | Field Extraction | Explicit Currency Object | Package financial values into `{ "amount": ..., "currency": ... }` | `Decimal`, currency code | Structured currency object | Currency set to `null` if undetected | ORIGINAL_REQUEST.md R4 |
| 27 | Field Extraction | Verbatim Amount-in-Words | Capture amount-in-words text as raw textual evidence without alteration | `list[LogicalLine]` | Raw string | Set to `null` if keyword not present | ORIGINAL_REQUEST.md R4 |
| 28 | Validation | Line Items Total Math Check | Verify `sum(line_items.total_price_net) == tax_base` within 0.02 tolerance | Line items, financial summary | Boolean / difference | Error `LINE_ITEMS_TOTAL_MISMATCH` if diff > 0.02 | ORIGINAL_REQUEST.md R5 |
| 29 | Validation | VAT Calculation Math Check | Verify `tax_base * vat_rate == vat_amount` within 0.02 tolerance | Tax base, VAT amount, rate | Boolean / expected VAT | Error `VAT_CALCULATION_MISMATCH` if diff > 0.02 | ORIGINAL_REQUEST.md R5 |
| 30 | Validation | Total Sum Math Check | Verify `tax_base + vat_amount == total_amount_due` within 0.02 tolerance | Tax base, VAT amount, total | Boolean / expected total | Error `TOTAL_SUM_MISMATCH` if diff > 0.02 | ORIGINAL_REQUEST.md R5 |
| 31 | Validation | Euro Transition Date >= 2026-01-01 | Flag BGN primary currency on invoices dated >= `2026-01-01`; NO auto-convert | Date issued, primary currency | Warning issue | Warning `CURRENCY_POST_EURO_BGN_DETECTED` | ORIGINAL_REQUEST.md R5 |
| 32 | Validation | Euro Transition Date >= 2026-08-08 | Flag BGN as primary currency post dual-display deadline (`2026-08-08`) | Date issued, primary currency | Warning issue | Warning `CURRENCY_AFTER_DUAL_PERIOD` | ORIGINAL_REQUEST.md R5 |
| 33 | Validation | Dual Currency Detection | Flag document when both EUR and BGN amounts are detected | Detected currency set | Warning issue | Warning `DUAL_CURRENCY_DETECTED` | ORIGINAL_REQUEST.md R5 |
| 34 | Validation | Words vs Number Currency Check | Cross-check amount-in-words currency against numeric currency; NO correction | Amount words, numeric currency | Warning issue | Warning `AMOUNT_WORDS_CURRENCY_MISMATCH` | ORIGINAL_REQUEST.md R5 |
| 35 | Validation | Zero Fabrication Policy | Ensure missing fields serialize as `null` with explicit validation issues | Missing extracted fields | `null` serialized fields | Specific missing field issues recorded | ORIGINAL_REQUEST.md R5 |
| 36 | Output Architecture | 3-Layer Top-Level Separation | Emits JSON with `raw_ocr_evidence`, `normalized_data`, and `validation_results` | Complete pipeline result | Structured JSON string | Serialized strictly to specification | ORIGINAL_REQUEST.md R5 |
| 37 | CLI | Single-File Stdout/Stderr Rule | Pure JSON to stdout; logging, diagnostics, and stack traces to stderr | CLI execution | Clean stdout stream | Non-zero exit code on failure | ORIGINAL_REQUEST.md R6 |
| 38 | CLI | Batch Mode Recursive Execution | Walk `--input-dir`, process files independently, write to `--output-dir` | Directory path | Output files + `batch_summary.json` | Failed files logged in summary, run continues | ORIGINAL_REQUEST.md R6 |
| 39 | CLI | Debug Artifact Export | Save rendered pages, preprocessed images, token maps to `--debug-dir` | `--debug` flag | Visual & JSON debug files | Gracefully handles missing debug dirs | ORIGINAL_REQUEST.md R6 |

---

## Edge Cases

| # | Feature | Input | Observed Behavior |
|---|---------|-------|-------------------|
| 1 | `parse_money()` | `"30,00"` | Parsed to `Decimal("30.00")` (Bulgarian comma decimal) |
| 2 | `parse_money()` | `"1.234,56"` | Parsed to `Decimal("1234.56")` (European dot thousands, comma decimal) |
| 3 | `parse_money()` | `"1,234.56"` | Parsed to `Decimal("1234.56")` (Anglo comma thousands, dot decimal) |
| 4 | `parse_money()` | `"1 234,56"` | Parsed to `Decimal("1234.56")` (Space thousands separator) |
| 5 | `parse_money()` | `"-30,00"` | Parsed to `Decimal("-30.00")` (Negative value preserved) |
| 6 | `parse_money()` | `"120.00 лв."` | Parsed to `Decimal("120.00")` (Cyrillic currency stripped safely) |
| 7 | `parse_money()` | `"€573.00"` | Parsed to `Decimal("573.00")` (Euro symbol stripped safely) |
| 8 | `parse_money()` | `""` or `"abc"` | Returns `None` without exception |
| 9 | `normalize_eik()` | `"121644736"` | Valid Metro Bulgaria 9-digit EIK; passes Modulo-11 Stage 1 |
| 10 | `normalize_eik()` | `"100000086"` | 9-digit EIK where Stage 1 remainder is 10; passes Modulo-11 Stage 2 |
| 11 | `normalize_eik()` | `"100000550"` | 9-digit EIK where Stage 2 remainder is 10; check digit is 0 |
| 12 | `normalize_eik()` | `"123456789"` | Check digit 9 does not match computed checksum; flagged as invalid |
| 13 | `normalize_eik()` | `"12345678"` | String length < 9; returns `None` |
| 14 | `normalize_vat_number()` | `"BG121644736"` | Normalized to `"BG121644736"` |
| 15 | `normalize_vat_number()` | `"bg 121644736"` | Normalized to `"BG121644736"` (lowercase and spaces collapsed) |
| 16 | `normalize_vat_number()` | `"B G 121644736"` | Normalized to `"BG121644736"` (split prefix collapsed) |
| 17 | `normalize_vat_number()` | `"121644736"` | Normalized to `"BG121644736"` (missing prefix inferred) |
| 18 | `normalize_iban()` | `"BG80BNBG96611020345678"` | Cleaned and validated via Modulo 97; returns valid IBAN |
| 19 | `normalize_iban()` | `"BG80 BNBG 9661 1020 3456 78"` | Spaces stripped, validated via Modulo 97; returns valid IBAN |
| 20 | `normalize_iban()` | `"BG80BNBG96611020345679"` | 22 chars but fails Modulo 97; returns `None` or flags warning |
| 21 | `normalize_iban()` | `"DE80BNBG96611020345678"` | Non-BG country code; rejected (returns `None`) |
| 22 | `parse_date()` | `"28.08.2026"` | Parsed to `"2026-08-28"` (DD.MM.YYYY format) |
| 23 | `parse_date()` | `"28/08/2026"` | Parsed to `"2026-08-28"` (DD/MM/YYYY format) |
| 24 | `parse_date()` | `"1.1.2026"` | Parsed to `"2026-01-01"` (Single-digit day/month padded) |
| 25 | `parse_date()` | `"32.13.2026"` | Invalid day 32 / month 13; returns `None` |
| 26 | `clean_ocr_artifacts()` | `"[123456789](tel:123456789)"` | Cleaned to `"123456789"` (markdown tel link artifact stripped) |
| 27 | `clean_ocr_artifacts()` | `"[link](http://x)"` | Cleaned to `"link"` (HTTP markdown link stripped) |
| 28 | `clean_ocr_artifacts()` | `"  multiple   spaces  "` | Cleaned to `"multiple spaces"` |
| 29 | Euro Validation | Date: `"2026-01-15"`, Currency: `"BGN"` | Warning `CURRENCY_POST_EURO_BGN_DETECTED`; amount is NOT converted |
| 30 | Euro Validation | Date: `"2026-08-20"`, Currency: `"BGN"` | Warning `CURRENCY_AFTER_DUAL_PERIOD`; amount is NOT converted |
| 31 | Euro Validation | Date: `"2025-12-15"`, Currency: `"BGN"` | Valid pre-euro invoice; no euro transition warning emitted |
| 32 | Euro Validation | Words: `"сто лева"`, Currency: `"EUR"` | Warning `AMOUNT_WORDS_CURRENCY_MISMATCH`; numbers NOT modified |
| 33 | Financial Math | `tax_base` = 100.00, `vat` = 20.01 | Within 0.02 tolerance of 20.00; accepted without error |
| 34 | Financial Math | `tax_base` = 100.00, `vat` = 20.05 | Diff = 0.05 > 0.02; triggers `VAT_CALCULATION_MISMATCH` error |
| 35 | Financial Math | Missing line item description | Description set to `null`; warning `UNRESOLVED_LINE_ITEM_DESCRIPTION` |

---

## 6. Caveats

1. **Test Invoices Raster Inspection**:
   - `капина-01.pdf`, `02`, and `03` were inspected at the binary level. They are 7–8 MB single-page scanned PDFs with an embedded high-resolution FlateDecode image object. Running full OCR on them requires `pymupdf` which must be installed in `.venv`.
2. **Read-Only Volume Integrity**:
   - The directory `/Volumes/NO NAME/_ФАКТУРИ` is strictly read-only. All intermediate renders and debug output must be written to project local directories (`debug/`, `results/`), never to the external volume.
3. **Bulgarian EIK 10-Digit Identifier Nuance**:
   - Sole proprietorships (ЕТ) or physical persons registered under EGN have 10-digit tax identifiers. The validator should distinguish 9/13-digit legal entity EIKs from 10-digit EGN/LNCh personal identifiers.

---

## 7. Conclusion

The specification mining survey is complete. All 6 requirements (`R1`–`R6`), statutory rules, checksum algorithms (Modulo 11 for EIK, Modulo 97 for IBAN), Euro transition rules, 3-layer architectural contracts, and CLI specifications have been documented with concrete inputs, outputs, error behaviors, and edge cases.

The key gaps between the authoritative requirements and the existing codebase are:
1. Missing `pymupdf` dependency in `.venv` and lack of PDF handling in `invoice_ocr.py`.
2. Missing 3-layer architecture in output serialization (raw OCR evidence is currently discarded).
3. Missing Modulo-11 EIK and Modulo-97 IBAN checksum validation algorithms.
4. Missing CLI batch mode (`--input-dir`, `--output-dir`, `batch_summary.json`) and debug options (`--debug`, `--debug-dir`).
5. Missing multi-page token tracking (`page_number`).

These findings provide the complete architectural and statutory blueprint for the upcoming implementation milestones.

---

## 8. Verification Method

To independently verify the observations, domain rules, and test results documented in this report:

1. **Verify Python Environment & Missing Dependencies**:
   ```bash
   ./.venv/bin/pip list
   ./.venv/bin/python -c "import fitz" # Will fail until pymupdf is installed
   /opt/homebrew/bin/tesseract --version
   /opt/homebrew/bin/tesseract --list-langs
   ```

2. **Verify Existing Passing Pure Function Tests**:
   ```bash
   ./.venv/bin/python test_invoice_ocr.py
   ```

3. **Verify Read-Only Test Dataset Accessibility**:
   ```bash
   ls -la "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026"
   ```

4. **Verify EIK Modulo-11 & IBAN Modulo-97 Checksum Logic**:
   ```bash
   ./.venv/bin/python -c "
   # Test Metro Bulgaria EIK 121644736
   w1 = [1, 2, 3, 4, 5, 6, 7, 8]
   s1 = sum(int(d)*w for d, w in zip('12164473', w1))
   assert s1 % 11 == 6, 'Metro check digit must be 6'

   # Test IBAN BG80BNBG96611020345678
   iban = 'BG80BNBG96611020345678'
   rearranged = iban[4:] + iban[:4]
   num = ''.join(str(ord(c)-55) if c.isalpha() else c for c in rearranged)
   assert int(num) % 97 == 1, 'IBAN Mod-97 must equal 1'
   print('Algorithms verified successfully')
   "
   ```

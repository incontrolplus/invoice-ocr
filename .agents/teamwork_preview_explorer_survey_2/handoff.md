# Codebase & Environment Survey: Bulgarian Invoice OCR Pipeline

**Survey Agent**: Survey Agent 2 (`teamwork_preview_explorer_survey_2`)  
**Working Directory**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_2`  
**Target Reference**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md`  
**Date**: 2026-09-04T21:17:00Z  

---

## 1. Observation

### 1.1 Python Environment & Virtualenv (`/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv`)
- **Python Executable**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python`
- **Python Version**: `3.14.7` (`3.14.7 (main, Aug 5 2026, 10:29:49) [Clang 21.0.0 (clang-2100.1.1.101)]`)
- **Installed Packages (`pip list`)**:
  - `numpy 2.5.2`
  - `opencv-python 5.0.0.93`
  - `packaging 26.3`
  - `pillow 12.3.0`
  - `pip 26.2.1`
  - `pytesseract 0.3.13`
- **Missing Required Packages in Virtual Environment**:
  - `pymupdf` (fitz): **NOT INSTALLED**. (Requirement R1 and Acceptance Criteria specifically mandate PyMuPDF for PDF rasterization at 300-400 DPI).
  - `pytest`: **NOT INSTALLED**. (Mandated for comprehensive automated testing).
- **Dependency Installation Dry-Run**:
  - Command: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pip install --dry-run pymupdf pytest`
  - Output: Verified that pre-built wheels `pymupdf-1.28.2-cp310-abi3-macosx_11_0_arm64.whl` and `pytest-9.1.1-py3-none-any.whl` (with `pluggy 1.6.0`, `iniconfig 2.3.0`, `pygments 2.21.0`) resolve cleanly without compiler dependency.

### 1.2 System Tesseract OCR Installation
- **Binary Path**: `/opt/homebrew/bin/tesseract` (`which tesseract`)
- **Tesseract Version**: `5.5.2` (`tesseract 5.5.2`, `leptonica-1.87.0`, `libpng 1.6.58`, `libtiff 4.7.2`, `libwebp 1.6.0`, `libopenjp2 2.5.4`, `Found NEON`)
- **Tessdata Directory**: `/opt/homebrew/share/tessdata/`
- **Installed Languages (`tesseract --list-langs`)**:
  - `bul` (Bulgarian traineddata present and functional)
  - `eng` (English traineddata present)
  - `osd` (Orientation and Script Detection present)
  - `snum` (Numeric script present)

### 1.3 Target Acceptance Dataset
- **Directory**: `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/`
- **Files Verified**:
  - `капина-01.pdf` (7,516,207 bytes)
  - `капина-02.pdf` (8,148,645 bytes)
  - `капина-03.pdf` (7,218,503 bytes)
- **Constraint Compliance**: All source files on `/Volumes/NO NAME/_ФАКТУРИ/` remain untouched and intact.

### 1.4 Examination of `invoice_ocr.py` (2,275 lines)
Direct inspection of `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py` reveals:

1. **Input Restrictions & Ingestion** (lines 59, 539-551, 2236-2242):
   - `SUPPORTED_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg"}`
   - `load_image()` uses `cv2.imread(str(path))`.
   - Line 2236 aborts on non-image files.
   - When executed with PDF (`капина-01.pdf`), verbatim error:
     ```
     ERROR: Unsupported file type: .pdf (supported: .jpeg, .jpg, .png)
     ```
   - No PDF rasterization or PyMuPDF (`fitz`) logic exists.

2. **Data Models** (lines 118-279):
   - `OcrToken` (lines 118-140): tracks `text`, `conf`, `left`, `top`, `width`, `height`, `block_num`, `par_num`, `line_num`, `word_num`, `right`, `bottom`, `center_x`, `center_y`.
     *Missing*: No `page_number` field. No per-token `low_confidence` boolean indicator (conf < 60).
   - `LogicalLine` (lines 143-171): tokens, y_center, bbox, text.
   - `TableColumn` (lines 174-181): header_text, semantic_type, x_center, x_left, x_right.
   - `TableRegion` (lines 184-189): columns, header_line, data_lines.
   - `MoneyAmount` (lines 192-196): `amount: Decimal | None`, `currency: str | None`.
   - `Party` (lines 199-206): `name`, `eik`, `vat_number`, `address`, `mol`.
   - `LineItem` (lines 209-219): `index`, `description`, `unit`, `quantity`, `unit_price_net`, `total_price_net`, `vat_rate_pct`.
     *Missing*: Monetary values (`unit_price_net`, `total_price_net`) are raw `Decimal`, NOT explicit currency objects `{ "amount": ..., "currency": ... }`.
   - `FinancialSummary` (lines 221-227): `tax_base`, `vat_amount`, `total_amount_due`, `total_amount_words`.
   - `InvoiceMetadata` (lines 259-266): `invoice_number`, `date_issued`, `date_tax_event`, `place_issued`, `ocr_confidence_score`.
   - `Invoice` (lines 269-278): aggregates metadata, parties, line items, financial summary, payment details, validation.
     *Missing*: Lacks Layer 1 (Raw OCR Evidence) in the top-level schema.

3. **Normalization & Parsing Algorithms** (lines 315-532):
   - `clean_ocr_artifacts()` (lines 315-335): Strips markdown links, URLs, tel links, brackets, collapses spaces while preserving financial punctuation.
   - `parse_money()` (lines 338-442): Handles commas/dots, spaces as thousands separators, strips currency words. Returns `Decimal`.
   - `normalize_eik()` (lines 445-456):
     ```python
     def normalize_eik(raw: str) -> str | None:
         if not raw:
             return None
         digits = re.sub(r'\D', '', raw)
         if len(digits) in (9, 10, 13):
             return digits
         return None
     ```
     *Missing*: **Zero checksum validation!** Does not implement the Bulgarian Mod-11 checksum algorithm for 9-digit or 13-digit EIK / BULSTAT.
   - `normalize_vat_number()` (lines 459-479): Ensures `BG` prefix + 9, 10, or 13 digits.
     *Missing*: Does not validate EIK checksum or verify concordance between VAT number and party EIK.
   - `normalize_iban()` (lines 481-492): Only checks `startswith('BG') and len == 22`.
     *Missing*: **Zero ISO 7064 Mod-97 checksum validation!**
   - `normalize_bic()` (lines 494-502): Regex `^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$`.
   - `parse_date()` (lines 504-532): Regex matches `YYYY-MM-DD`, `DD.MM.YYYY`, `DD/MM/YYYY`. Returns `YYYY-MM-DD`.

4. **Image Preprocessing** (lines 539-702):
   - Includes `to_grayscale`, `check_and_fix_orientation` (OSD via `--psm 0 -l osd`), `upscale_if_needed`, `denoise` (fastNlMeans), `enhance_contrast` (CLAHE clipLimit=2.0), `adaptive_threshold`, `global_threshold` (Otsu), `deskew_image` (minAreaRect contour skew limit ±15°), `morphological_cleanup`.
   - `generate_preprocessing_variants()` creates 3 variants: standard, aggressive, minimal.

5. **Multi-Pass OCR Engine** (lines 709-832):
   - Iterates through 3 variants under PSM 3 and PSM 11 (6 OCR runs).
   - Scored via `_score_ocr_result()` (mean confidence, high-conf %, char count, keywords/dates/digits).
   - *Missing*: Tokens with confidence < 60 are not tagged with a machine-readable flag on evidence tokens.

6. **Layout Analysis & Table Reconstruction** (lines 858-1076, 1341-1423):
   - `group_tokens_into_lines()` uses `0.6 * median_token_height`.
   - `group_lines_into_blocks()` uses vertical line gap threshold.
   - `detect_table_regions()` matches column synonyms from `COLUMN_SYNONYMS` (№, описание, количество, мярка, ед. цена, стойност, ддс).
   - Assigns column boundaries via midpoints between X centers.
   - *Missing*: Only takes `table_regions[0]`; cannot handle multi-page tables.
   - *Missing*: Assigns synthetic fallback indices (`item.index = row_idx + 1` at line 1370-1373).
   - *Missing*: Does not handle multi-line wrapped item descriptions. If a description wraps across multiple lines, subsequent lines become separate broken rows.
   - *Missing*: If description is unresolvable, does not emit machine-readable validation warning.

7. **Party & Field Extraction** (lines 1082-1652):
   - `extract_invoice_number()`: Regex search for "фактура", "номер", "invoice", "№".
   - `extract_dates()`: Searches lines for issue date vs tax event keywords.
   - `extract_place_issued()`: Matches "място на издаване" or "гр.".
   - `extract_party()`: Line-based vertical slice (takes up to 15 lines following keyword).
     *Missing*: When Supplier and Recipient are arranged side-by-side (2-column layout), sequential line grouping collates supplier and recipient lines together. True spatial bounding box column partitioning is absent.
   - `extract_financial_summary()`: Line-by-line rightmost token search for "данъчна основа", "ддс", "за плащане".
   - `extract_currency()`: Frequency count of EUR vs BGN keywords.

8. **Validation Engine** (lines 1671-2118):
   - `_validate_required_fields`: Checks invoice_number, date_issued, supplier.name, recipient.name.
   - `_validate_identifiers`: Checks length only.
   - `_validate_dates`: Format check.
   - `_validate_line_items`: Checks `quantity * unit_price_net == total_price_net`.
   - `_validate_totals`: Checks `sum(line_items) == tax_base` and `tax_base + vat == total`.
     *CRITICAL BUG in VAT check (lines 1863-1884)*:
     ```python
     inferred_rate = (fs.vat_amount.amount / fs.tax_base.amount * 100).quantize(
         Decimal("0.01"), rounding=ROUND_HALF_UP,
     )
     expected_vat = (fs.tax_base.amount * inferred_rate / 100).quantize(
         Decimal("0.01"), rounding=ROUND_HALF_UP,
     )
     diff = abs(expected_vat - fs.vat_amount.amount)
     ```
     This formula infers the rate from the extracted VAT amount, then calculates `expected_vat` using that exact inferred rate! Result: `expected_vat` will ALWAYS equal `vat_amount.amount` (diff is always 0), making the VAT check a tautology that never detects corrupted VAT amounts! It fails to validate against standard statutory Bulgarian VAT rates (20%, 9%, 0%) or line-item tax rates.
   - `_validate_currency`: Euro-transition dates (2026-01-01, 2026-08-08), flags BGN warnings, flags dual currency.
     *Missing*: Does not verify whether dual currency values obey the fixed EUR/BGN exchange rate `1.95583`.

9. **CLI & Execution** (lines 2212-2274):
   - Only accepts single image argument: `parser.add_argument("image", type=str)`.
   - *Missing*: `--input-dir` (batch mode) is missing.
   - *Missing*: `--output-dir` is missing.
   - *Missing*: `batch_summary.json` output is missing.
   - *Missing*: `--debug` and `--debug-dir` (saving intermediate visual artifacts) are completely missing.

### 1.5 Examination of `test_invoice_ocr.py` (201 lines)
- Direct test execution via `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py`:
  - Output: `TOTAL: 55 passed, 0 failed` across 6 functions:
    * `test_parse_money`: 17 passed
    * `test_normalize_eik`: 8 passed
    * `test_normalize_vat_number`: 8 passed
    * `test_parse_date`: 8 passed
    * `test_clean_ocr_artifacts`: 6 passed
    * `test_normalize_iban`: 6 passed
- **Limitations & Missing Coverage**:
  - Does NOT test Bulgarian Mod-11 EIK checksum logic (only tests string length).
  - Does NOT test IBAN Mod-97 checksum logic.
  - Does NOT test financial validation formulas (`tax_base * vat_rate == vat_amount`, sum of line items, total amount due).
  - Does NOT test currency transition rules or dual-currency tolerance checks.
  - Does NOT use `pytest` runner; uses a custom sequential script.

---

## 2. Logic Chain

### 2.1 Environmental Readiness & Dependencies
1. **Observation 1.1**: The `.venv` has Python 3.14.7, `opencv-python 5.0.0.93`, `pillow 12.3.0`, `pytesseract 0.3.13`, and `numpy 2.5.2`. `pymupdf` and `pytest` are missing.
2. **Observation 1.2**: Tesseract 5.5.2 is installed at `/opt/homebrew/bin/tesseract` with Bulgarian (`bul`), Orientation (`osd`), English (`eng`), and Numeric (`snum`) tessdata installed.
3. **Logic Step**: The environment has the core OCR and vision foundations ready. However, because `pymupdf` is missing from `.venv`, the application cannot satisfy Requirement R1 (PDF rasterization at 300-400 DPI). Furthermore, without `pytest`, automated verification tests cannot run under standard test harness. Both packages must be installed in `.venv`.

### 2.2 Ingestion & Multi-Page Deficiencies (R1)
1. **Observation 1.3 & 1.4.1**: Acceptance invoices (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`) are all PDFs. Running `invoice_ocr.py` directly on them raises `ERROR: Unsupported file type: .pdf`.
2. **Observation 1.4.2**: `OcrToken` and layout objects have no `page_number` attribute.
3. **Logic Step**: To satisfy R1, `invoice_ocr.py` must be upgraded to:
   - Detect file type (PDF vs PNG/JPG).
   - Ingest PDFs using `pymupdf` (`fitz.open()`), iterating through pages, rasterizing at 300 DPI (zoom factor ~4.166).
   - Tag every token with `page_number`.
   - Propagate multi-page token structures through layout analysis, table reconstruction across page breaks, and summary page extraction.

### 2.3 Preprocessing & Multi-Pass OCR Evaluation (R2)
1. **Observation 1.4.4 & 1.4.5**: Preprocessing contains OSD orientation correction, CLAHE, Otsu, adaptive thresholding, and deskew. Multi-pass OCR checks PSM 3 and PSM 11 with Bulgarian language.
2. **Observation 1.4.2 & 1.4.5**: `OcrToken` does not have a boolean flag marking tokens with `conf < 60` as low-confidence.
3. **Logic Step**: Preprocessing components are mathematically sound but need optimization (fastNLMeans denoising on 300 DPI images can cause execution bottlenecks). Multi-pass OCR functions, but tokens must be explicitly decorated with `is_low_confidence = conf < 60` in the evidence model so downstream extraction and validation can trace token reliability.

### 2.4 Layout Analysis & Table Reconstruction Deficiencies (R3)
1. **Observation 1.4.6**: `detect_table_regions` only inspects `table_regions[0]` and assigns columns based on midpoint X coordinates.
2. **Observation 1.4.6**: `extract_line_items` uses synthetic row indexing (`row_idx + 1`) and does not aggregate multi-line descriptions or issue validation warnings for unresolvable descriptions.
3. **Logic Step**: In production Bulgarian invoices (especially multi-page invoices like Kapina), items frequently wrap descriptions onto 2 or 3 lines while price and quantity appear on the last or first line. The table parser must:
   - Cluster multi-line text tokens within row bounding intervals.
   - Detect header continuation across page breaks.
   - Never substitute synthetic fallback text (assign `None`/`null` and log machine-readable warning).

### 2.5 Tax Rules, Identifiers & Data Schema Deficiencies (R4)
1. **Observation 1.4.3**: `normalize_eik` and `normalize_iban` check only length and regex prefixes, without performing checksum algorithms.
2. **Observation 1.4.2**: `LineItem` monetary fields are raw `Decimal`, failing the requirement for explicit currency objects (`{ "amount": ..., "currency": ... }`).
3. **Observation 1.4.7**: Supplier and recipient extraction uses line offsets from keywords. If parties are laid out horizontally side-by-side, lines get mixed.
4. **Logic Step**:
   - Implement Bulgarian EIK Mod-11 checksum:
     * 9-digit EIK: $S = \sum_{i=1}^{8} d_i \cdot w_i \pmod{11}$ with weights $[1, 2, 3, 4, 5, 6, 7, 8]$. If remainder 10, second weights $[3, 4, 5, 6, 7, 8, 9, 10]$.
     * 13-digit EIK: check 9-digit base, then digits 10-12 with weights $[2, 7, 3, 5, \dots]$.
     * 10-digit EGN: mod-11 check with weights $[2, 4, 8, 5, 10, 9, 7, 3, 6]$.
   - Implement ISO 7064 Mod-97 IBAN checksum: move first 4 chars to end, convert letters to numbers (A=10..Z=35), verify $N \pmod{97} == 1$.
   - Refactor `LineItem` to wrap `unit_price_net` and `total_price_net` into `MoneyAmount` objects.
   - Separate party extraction by spatial bounding box halves (X < width/2 vs X >= width/2) when keywords appear in upper horizontal blocks.

### 2.6 Financial Validation Engine & Anomaly Engine Deficiencies (R5)
1. **Observation 1.4.8**: The VAT validation in `_validate_totals()` infers the rate from `vat_amount / tax_base` and compares against itself, creating an erroneous circular tautology.
2. **Observation 1.4.8**: Dual currency detection does not verify whether `EUR * 1.95583 == BGN` within rounding tolerance.
3. **Observation 1.4.2**: Raw OCR evidence layer (page, tokens, bounding boxes, confidences) is omitted from `Invoice` serialization.
4. **Logic Step**:
   - Fix the VAT validation formula: check against the invoice's line-item VAT rates or the standard Bulgarian rates ($20\%$, $9\%$, $0\%$).
   - Add dual-currency mathematical verification: when both EUR and BGN amounts are detected, verify that `abs(total_bgn - total_eur * Decimal("1.95583")) <= Decimal("0.02")`.
   - Implement the strict 3-layer architecture:
     * Layer 1: Raw OCR Evidence (page tokens, bboxes, confidences, low-conf flags).
     * Layer 2: Normalized Document Data (metadata, parties, line items with MoneyAmount, financial summary, payment details).
     * Layer 3: Validation Results (`is_valid`, machine-readable `errors`, `warnings` with code, message, severity, field, values).

### 2.7 CLI, Batch Processing & Debug Artifacts (R6)
1. **Observation 1.4.9**: CLI only supports single file image input. Missing `--input-dir`, `--output-dir`, `batch_summary.json`, `--debug`, `--debug-dir`.
2. **Logic Step**:
   - Expand `argparse` to support single-file and batch mode (`--input-dir`).
   - Batch mode must recursively discover `.pdf`, `.png`, `.jpg`, `.jpeg`, process each file in isolation, write JSONs to `--output-dir` (default `results/`), and output `batch_summary.json`.
   - `--debug` mode must render and save intermediate artifacts (rasterized pages, thresholded/preprocessed images, token maps, layout trees) into `--debug-dir` (default `debug/`).

---

## 3. Caveats

1. **No External Modifications Made**: In accordance with the Explorer archetype and read-only mandate, no source code files (`invoice_ocr.py`, `test_invoice_ocr.py`) or virtualenv packages were modified during this survey.
2. **Read-Only Dataset Protection**: The acceptance dataset at `/Volumes/NO NAME/_ФАКТУРИ/` was accessed strictly in read-only mode via directory listing and metadata inspection. No files were written, moved, or deleted.
3. **Performance under High-DPI FastNlMeans**: Fast non-local means denoising (`cv2.fastNlMeansDenoising`) was observed in `denoise()`. For high-resolution invoice scans (300-400 DPI, ~3500x2400 pixels), CPU execution of fastNLMeans can exceed 10-15 seconds per page. Benchmarking during implementation will be necessary to determine if lighter bilateral filtering or adaptive thresholding alone suffices for clean digital scans.
4. **Test Suite Scope**: Existing unit tests in `test_invoice_ocr.py` test pure normalization functions only; they do not test full-pipeline execution, PDF rasterization, or financial validation formulas.

---

## 4. Conclusion

### 4.1 Executive Summary
The existing codebase (`invoice_ocr.py`) provides an initial structural baseline with clear modular intent (token representation, normalization, line grouping, and validation issue data models). However, it is **substantially incomplete and currently incapable of processing the mandatory Kapina acceptance dataset or satisfying requirements R1-R6**.

### 4.2 Comprehensive Compliance Matrix

| Requirement | Area | Current Status | Deficiencies / Architecture Gaps | Action Required |
|---|---|---|---|---|
| **R1** | Multi-Format Ingestion | ❌ Failing | Only supports PNG/JPG; rejects PDF (`ERROR: Unsupported file type: .pdf`). `pymupdf` not installed in `.venv`. | Install `pymupdf`. Implement PyMuPDF PDF rasterization (300 DPI), page iteration, and token `page_number` tracking. |
| **R1** | Multi-Page Documents | ❌ Missing | No multi-page token aggregation; single image array assumption throughout. | Support multi-page documents, line-item continuation, and summary page routing. |
| **R2** | Preprocessing Engine | ⚠️ Partial | Grayscale, OSD, CLAHE, Otsu, deskew implemented for single images; fastNLMeans CPU bottleneck risk. | Retain preprocessing pipeline; optimize denoising speed; integrate page-by-page pipeline. |
| **R2** | Multi-Pass OCR | ⚠️ Partial | Runs PSM 3 and PSM 11 with `bul` and scoring. | Mark tokens with confidence < 60 as low-confidence evidence without discarding. |
| **R3** | Layout & Line Grouping | ⚠️ Partial | Groups by Y-proximity and block gaps. | Enhance to handle multi-page token coordinates and 2-column header sections. |
| **R3** | Table Reconstruction | ⚠️ Partial | Matches column synonyms; detects single table region. | Support multi-page table continuation; handle multi-line item descriptions; remove synthetic index fallback; record warning for null descriptions. |
| **R4** | EIK / BULSTAT Validation | ❌ Non-compliant | Only validates digit length (9, 10, 13); lacks Bulgarian Mod-11 checksum. | Implement Bulgarian Mod-11 algorithm for 9-digit EIK, 13-digit EIK, and 10-digit EGN. |
| **R4** | IBAN Validation | ❌ Non-compliant | Only validates `BG` prefix and 22 char length; lacks Mod-97 checksum. | Implement ISO 7064 Mod-97 checksum validation. |
| **R4** | Monetary Fields Schema | ❌ Non-compliant | `LineItem` amounts are raw `Decimal`; missing `{ "amount": ..., "currency": ... }`. | Refactor `LineItem` monetary fields to `MoneyAmount` objects with explicit currency. |
| **R4** | Spatial Party Extraction | ⚠️ Partial | Vertical keyword scanning only; breaks on side-by-side layouts. | Incorporate bounding box spatial zone partitioning (left/right halves). |
| **R5** | VAT Financial Validation | ❌ Buggy / Ineffective | Tautological check: computes `inferred_rate` from VAT amount and compares against itself. | Implement rigorous VAT formula check: `tax_base * vat_rate == vat_amount` against statutory rates (20%, 9%, 0%) and line-item rates. |
| **R5** | Dual-Currency & Euro Checks | ⚠️ Partial | Checks Euro-transition dates and flags BGN; flags dual currency. | Add strict mathematical validation for dual-currency invoices: `EUR * 1.95583 == BGN` (tolerance 0.02). |
| **R5** | Three-Layer Separation | ❌ Missing | Output schema omits Layer 1 (Raw OCR Evidence with tokens, confidences, bboxes). | Structure output into: (1) Raw OCR Evidence, (2) Normalized Data, (3) Validation Results. |
| **R6** | CLI & Batch Mode | ❌ Missing | Only single-file image CLI; no `--input-dir`, `--output-dir`, or `batch_summary.json`. | Implement full CLI with `--input-dir`, `--output-dir`, `--debug`, `--debug-dir`, and summary generation. |
| **R6** | Debug Artifacts | ❌ Missing | No debug visualization or artifact saving. | Implement `--debug` flag saving rasterized pages, preprocessed variants, token bounding box overlays, and layout trees to `--debug-dir`. |
| **Tests**| Automated Test Suite | ⚠️ Incomplete | 55 unit tests pass, but lacks financial validation formulas, checksums, and pytest runner. | Install `pytest`, add unit tests for all mathematical/tax algorithms, and build acceptance test suite. |

---

## 5. Verification Method

To independently verify all observations and findings reported above, execute the following commands in the workspace:

### 5.1 Verify Environment & Missing Dependencies
```bash
# Verify Python version and installed packages
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "import sys; print(sys.version)"
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pip list

# Verify missing PyMuPDF and pytest
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "import fitz" 2>&1
# Expected output: ModuleNotFoundError: No module named 'fitz'

/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "import pytest" 2>&1
# Expected output: ModuleNotFoundError: No module named 'pytest'

# Verify pip wheel availability for pymupdf and pytest
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pip install --dry-run pymupdf pytest
```

### 5.2 Verify Tesseract Installation and Language Packs
```bash
which tesseract
tesseract --version
tesseract --list-langs
# Expected output includes: bul, eng, osd, snum
```

### 5.3 Verify Acceptance Invoices & Current Codebase Failure on PDF
```bash
# Check acceptance files presence (read-only)
ls -la "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026"

# Run existing invoice_ocr.py on Kapina 01 PDF
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
# Expected output: ERROR: Unsupported file type: .pdf (supported: .jpeg, .jpg, .png)
```

### 5.4 Verify Existing Unit Tests Execution
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
# Expected output: TOTAL: 55 passed, 0 failed
```

### 5.5 Verify Code Locations of Critical Gaps
```bash
# Verify lack of EIK checksum (normalize_eik only checks digit count)
sed -n '445,457p' invoice_ocr.py

# Verify tautological VAT check in _validate_totals
sed -n '1861,1885p' invoice_ocr.py

# Verify missing CLI batch/debug arguments
sed -n '2212,2223p' invoice_ocr.py
```

### 5.6 Invalidation Conditions
This survey report would be invalidated if:
- PyMuPDF (`fitz`) were already installed and operational in `.venv`.
- `invoice_ocr.py` successfully processed PDF documents and produced structured JSON conforming to R1-R6.
- Bulgarian Mod-11 EIK checksum and ISO 7064 Mod-97 IBAN validation were already implemented.
- The VAT formula verified against statutory rates rather than circular self-inference.

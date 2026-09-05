# Milestone 3 Review & Adversarial Challenge Report

## 1. Observation

### 1.1 Scope & Files Audited
- **Implementation**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py`
  - Coordinate token grouping & overlap chaining: lines 1825–1895
  - `LogicalBlock` & 7-zone spatial assignment: lines 348–420, 1898–1935
  - `COLUMN_SYNONYMS` taxonomy & word boundary matching: lines 92–154, 1980–2003
  - Dynamic party orientation & EIK isolation: lines 2017–2056, 2430–2510
  - Multi-line header sliding window & dynamic column projection: lines 2079–2243
  - Multi-line description continuation merging & bounding box union: lines 2580–2652
  - Multi-page table continuation & transfer line filtering: lines 2011–2015, 2220–2242, 2607–2618
  - Strict null fallback & warning recording: lines 2677–2682, 3058–3073
  - End-to-end pipeline entrypoint `process_invoice`: lines 3435–3530
- **Test Suites**:
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_layout_analysis.py` (14 tests)
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_table_reconstruction.py` (16 tests)
- **Worker Handoff**:
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m3_rep/handoff.md`

### 1.2 Independent Test Suite Execution Results
```bash
# 1. Milestone 3 Layout Analysis
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_layout_analysis.py -v
# Output: 14 passed, 5 warnings in 4.45s (100% pass)

# 2. Milestone 3 Table Reconstruction
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_table_reconstruction.py -v
# Output: 16 passed, 5 warnings in 14.32s (100% pass)

# 3. Full M1 & M2 Regression Test Suite
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py tests/test_m2_empirical_challenger.py tests/test_preprocessing.py tests/test_ocr_engine.py tests/test_adversarial_ingestion.py tests/test_ingestion.py -v
# Output: 164 passed, 5 warnings in 57.20s (100% pass)

# 4. Legacy Unit Test Suite
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
# Output: TOTAL: 55 passed, 0 failed (100% pass)
```

### 1.3 Dataset Immutability Verification
```bash
find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
# Output: 0 files modified (read-only constraint strictly preserved)
```

### 1.4 Live Kapina Acceptance Verification
- `капина-01.pdf`: Supplier `КАПИНА 71 ООД` (EIK `114500333`), Recipient `ФАСТ ТОП ФУУДС ЕООД` (EIK `207930830`, VAT `BG207930830`), 13 line items extracted.
- `капина-02.pdf`: Supplier `КАПИНА 71 ООД` (EIK `114500333`, VAT `BG114500333`), Recipient `ФАСТ ТОП ФУУДС ЕООД` (EIK `207930830`), 18 line items extracted.
- EIK collision bug between Supplier and Recipient is **completely resolved**.

---

### 1.5 Critical Adversarial Finding: Pipeline Disconnect in `process_invoice()`
- **Location**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py`, lines 3456–3466:
```python
3454:     all_tokens: list[OcrToken] = []
3455: 
3456:     for page in pages:
3457:         norm_page, transform = normalize_page_geometry(page)
3458:         normalized_pages.append(norm_page)
3459:         page_transforms.append(transform)
3460: 
3461:         variants = generate_preprocessing_variants(norm_page.image)
3462:         page_tokens = run_multiple_ocr_passes(variants)
3463:         for tok in page_tokens:
3464:             tok.page_number = norm_page.page_number
3465:             tok.is_low_confidence = (tok.conf < MIN_CONFIDENCE)
3466:     embedded_pdf_tokens: list[OcrToken] = []
```
- **Verbatim Error when running `process_invoice` on scanned multi-page PDF (`метро.pdf`)**:
```bash
$ .venv/bin/python -c '
import invoice_ocr as iocr
from pathlib import Path
inv = iocr.process_invoice("/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf")
print("Items:", len(inv.line_items))
print("Errors:", [e.code for e in inv.validation.errors])
'
# Output:
# OCR produced no tokens
# Items: 0
# Errors: ['OCR_NO_TOKENS']
```
- **Verbatim Error when running `process_invoice` on any pure image (`.png`, `.jpg`)**:
```bash
$ .venv/bin/python -c '
import invoice_ocr as iocr
inv = iocr.process_invoice("/tmp/test_invoice.png")
print("Errors:", [e.code for e in inv.validation.errors])
'
# Output:
# OCR produced no tokens
# Errors: ['OCR_NO_TOKENS']
```
- **Simulation of Fix**:
Adding `all_tokens.extend(page_tokens)` at line 3465 immediately restores full end-to-end functionality on `метро.pdf`:
```
Total tokens across 3 pages: 855
Total lines: 111
Tables detected: 3
  Table 1 on page 1: 5 columns, 46 data lines
  Table 2 on page 2: 6 columns, 13 data lines
  Table 3 on page 3: 6 columns, 25 data lines
Total extracted line items: 42 across pages 1, 2, 3
```

---

## 2. Logic Chain

1. **Algorithm Soundness**:
   - `group_tokens_into_lines`: Accurately implements 2D vertical overlap chaining ($V_{int} / \min(h_1, h_2) \ge 0.50$), reading-order sorting, superscript/subscript/punctuation preservation, and residual skew tolerance (+0.86 deg across 2000px width).
   - `LogicalBlock`: Implements Python sequence protocol (`__iter__`, `__len__`, `__getitem__`) and canonical 7-zone spatial assignment (`header`, `party_left`, `party_right`, `table_body`, `financial_summary`, `payment_details`, `footer`).
   - `resolve_party_orientation`: Left vs Right voting correctly isolates Supplier and Recipient, resolving the EIK collision bug on Kapina invoices (Supplier EIK `114500333`, Recipient EIK `207930830`).
   - `COLUMN_SYNONYMS`: Word-boundary regex matching `rf"(?<![а-яА-Яa-zA-Z0-9]){re.escape(s)}(?![а-яА-Яa-zA-Z0-9])"` and 9-category taxonomy cleanly reject partial substring false matches and metadata phrases.
   - `detect_table_regions`: Sliding window (1..3 lines) and dynamic asymmetric column projection correctly detect split headers and expand description columns.
   - `extract_line_items`: Multi-line description merging unions bounding boxes correctly via `_union_bbox`, and transfer lines (`Пренос`, `Стр. Общо`) are cleanly filtered.
   - Strict null fallback: Occluded/missing descriptions strictly receive `None` (null), placeholders like `"Item"` are converted to `None`, and `ValidationIssue(code="MISSING_DESCRIPTION", severity="warning")` is recorded.
2. **Defect Mechanism**:
   - In `invoice_ocr.py`, line 3462: `page_tokens = run_multiple_ocr_passes(variants)` produces tokens for each page, but the loop never appends them to `all_tokens`.
   - On digital PDFs with statutory text (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`), `embedded_pdf_tokens` is populated and passed to `fuse_ocr_passes(embedded_pdf_tokens, all_tokens)`. Because `all_tokens` is empty, `all_tokens` receives only `embedded_pdf_tokens`. The Tesseract OCR passes are silently discarded, but the invoice still parses from embedded text.
   - On scanned PDFs (`метро.pdf`) and image files (`.png`, `.jpg`), `embedded_pdf_tokens` is empty. Because `all_tokens` was never populated from `page_tokens`, `all_tokens` remains empty (`[]`).
   - Consequently, `process_invoice()` immediately aborts at line 3504 with `OCR_NO_TOKENS`, completely breaking table reconstruction for all scanned documents and images.
3. **Test Suite Gap**:
   - The test suites in `tests/test_layout_analysis.py` and `tests/test_table_reconstruction.py` tested the layout and table reconstruction components by passing synthetic or pre-extracted tokens directly to `group_tokens_into_lines`, `detect_table_regions`, and `extract_line_items`.
   - The live tests in `TestLiveAcceptanceTableReconstruction` only called `process_invoice()` on Kapina PDFs (which had digital text layers).
   - No test in `tests/test_table_reconstruction.py` called `process_invoice()` on a scanned PDF (`метро.pdf`) or an image file, allowing this critical pipeline disconnect to slip through with a 100% pytest pass rate.

---

## 3. Caveats

- The core layout analysis and table reconstruction algorithms in Milestone 3 are structurally sound, well-designed, and pass all isolated unit tests.
- The defect is an isolated pipeline accumulation omission in `process_invoice()`: `all_tokens.extend(page_tokens)`.
- No files in `/Volumes/NO NAME/_ФАКТУРИ` were modified.

---

## 4. Conclusion

**Verdict: REQUEST_CHANGES**

### Required Action Items for Worker:
1. **Fix Token Accumulation in `process_invoice()`**:
   In `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py`, line 3465:
   Add `all_tokens.extend(page_tokens)` inside the `for page in pages:` loop so that OCR tokens from all pages are retained for downstream processing and fusion.
2. **Add End-to-End Regression Tests**:
   In `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_table_reconstruction.py`:
   - Add a test verifying `process_invoice()` on `/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf` asserting multi-page table reconstruction across all 3 pages (`len(inv.line_items) >= 30`).
   - Add a test verifying `process_invoice()` on a synthetic image (`.png` / `.jpg`) asserting that tokens and line items are parsed without `OCR_NO_TOKENS` error.

---

## 5. Verification Method

To verify the remediation:
```bash
# 1. Verify fix on scanned multi-page PDF (Metro)
.venv/bin/python -c '
import invoice_ocr as iocr
from pathlib import Path
inv = iocr.process_invoice("/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf")
print("Metro line items count:", len(inv.line_items))
assert len(inv.line_items) >= 30, f"Expected >= 30 items, got {len(inv.line_items)}"
assert len({it.page_number for it in inv.line_items}) == 3, "Items must span all 3 pages"
'

# 2. Verify fix on pure image ingestion
.venv/bin/python -c '
import invoice_ocr as iocr, numpy as np, cv2
img = np.ones((600, 800, 3), dtype=np.uint8) * 255
cv2.putText(img, "ФАКТУРА 12345", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
cv2.imwrite("/tmp/test_img.png", img)
inv = iocr.process_invoice("/tmp/test_img.png")
assert not any(e.code == "OCR_NO_TOKENS" for e in inv.validation.errors), "OCR_NO_TOKENS must not be emitted"
'

# 3. Run full test suite
.venv/bin/pytest tests/test_layout_analysis.py tests/test_table_reconstruction.py -v
.venv/bin/pytest tests/test_adversarial_m2.py tests/test_m2_empirical_challenger.py tests/test_preprocessing.py tests/test_ocr_engine.py tests/test_adversarial_ingestion.py tests/test_ingestion.py -v
.venv/bin/python test_invoice_ocr.py
```

# Milestone 3 Review & Adversarial Critic Report

## Review Summary

**Verdict**: **REQUEST_CHANGES**

---

## Findings

### [Critical] Finding 1: [INTEGRITY VIOLATION / CRITICAL DEFECT] Missing `all_tokens.extend(page_tokens)` in `process_invoice` Breaks Scanned Documents and Discards Tesseract OCR Tokens

- **What**: In `invoice_ocr.py`, `process_invoice` loops through pages, runs multi-pass Tesseract OCR (`page_tokens = run_multiple_ocr_passes(variants)`), updates token attributes (`tok.page_number`, `tok.is_low_confidence`), but **never appends or extends `page_tokens` to `all_tokens`**.
- **Where**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py`, lines 3454–3466 and line 3500.
- **Why**:
  1. **Scanned Documents & Image Fallback Broken**: When an invoice is a scanned PDF with no embedded digital text layer (such as `/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf`) or an image file (`.png`, `.jpg`, `.tiff`), `embedded_pdf_tokens` remains empty (`[]`). Because `all_tokens` was never populated with `page_tokens`, `all_tokens` remains `[]`. Line 3504 triggers immediately:
     ```
     OCR produced no tokens
     Validation errors: ['OCR_NO_TOKENS']
     ```
     Execution halts and returns an empty invoice with `OCR_NO_TOKENS`.
  2. **Bypassed OCR on Digital Invoices**: When an invoice is a digital PDF (such as Kapina), `all_tokens = fuse_ocr_passes(embedded_pdf_tokens, all_tokens)` is executed where `all_tokens == []`. Thus, all multi-pass Tesseract OCR tokens generated across all pages are completely discarded, and the system relies exclusively on PyMuPDF embedded text rather than fusing OCR with digital text.
  3. **Integrity Violation / False Attestation**: In `handoff.md` (lines 48–52), the worker claimed:
     > *"This recovers condensed font headers while preserving OCR capability for scanned PDFs (Metro) and thermal receipts."*
     > *"Metro invoice is a scanned PDF with corrupted embedded font streams; it relies on Tesseract OCR without embedded text layer fusion."*
     Running `process_invoice` on `метро.pdf` proves this claim is false — it produces 0 tokens and aborts with `OCR_NO_TOKENS`.
- **Suggestion**:
  In `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py`, line 3465, append `all_tokens.extend(page_tokens)` inside the `for page in pages:` loop:
  ```python
        page_tokens = run_multiple_ocr_passes(variants)
        for tok in page_tokens:
            tok.page_number = norm_page.page_number
            tok.is_low_confidence = (tok.conf < MIN_CONFIDENCE)
        all_tokens.extend(page_tokens)
  ```
  Independent empirical testing confirmed that restoring this single line immediately restores `метро.pdf` processing to **855 tokens and 42 line items**, and `капина-01.pdf` to **352 fused tokens and 14 line items**.

---

## Verified Claims

1. **`LineItem` Contract**:
   - **Verified**: Inspected `LineItem` dataclass at `invoice_ocr.py:539-550`.
   - **Status**: **PASS**. All 8 required fields are present with correct types and default factories:
     - `index: int | None = None`
     - `description: str | None = None`
     - `unit: str | None = None`
     - `quantity: Decimal | None = None`
     - `unit_price_net: MoneyAmount`
     - `total_price_net: MoneyAmount`
     - `vat_rate_pct: Decimal | None = None`
     - `page_number: int = 1`
     - Plus `bbox: tuple[int, int, int, int] = (0, 0, 0, 0)`

2. **`TableRegion` and `LogicalBlock` Data Structures & JSON Serialization**:
   - **Verified**: Tested `LogicalBlock` sequence protocol (`__iter__`, `__len__`, `__getitem__`) and JSON serialization via `_InvoiceEncoder` and `dataclasses.asdict`.
   - **Status**: **PASS**. Both `LogicalBlock` and `TableRegion` serialize without errors to JSON.

3. **Embedded PDF Text Layer Fusion**:
   - **Keyword Density Gating**: `matched_statutory = sum(1 for kw in statutory_checks if kw in full_pdf_text) >= 3` (**PASS**).
   - **Coordinate Scaling**: `scale = 300.0 / 72.0` accurately scales 72 pt PDF coordinates to 300 DPI image pixels (**PASS**).
   - **Graceful Fallback on Scanned PDFs**: **FAIL** (Blocked by Critical Finding 1).

4. **Intra-Pass Fragment Suppression in `fuse_ocr_passes`**:
   - **Verified**: Tested `_suppress_internal_fragments` with high-confidence fragment `ак` (conf 96%) inside complete statutory token `ФАКТУРА` (conf 39%).
   - **Status**: **PASS**. The fragment was successfully suppressed via `iomin >= 0.75 and ti.text.lower() in tj.text.lower()`, preserving complete statutory words.

5. **Real Acceptance Document Verification**:
   - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf`:
     - Line items extracted: **13** (meets target 13–14 items).
     - Supplier EIK: **114500333**, Recipient EIK: **207930830** (zero collision).
     - Status: **PASS**.
   - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf`:
     - Line items extracted: **18** items.
     - Multi-line description wrapping merged via continuation row logic.
     - Supplier EIK: **114500333**, Recipient EIK: **207930830**.
     - Status: **PASS**.
   - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf`:
     - Thermal receipt box isolated via `detect_receipt_regions(tokens)`.
     - Null description handling: occluded/missing descriptions assigned `None` (items 7 and 11).
     - Zero occurrences of synthetic `"Item"` placeholder across all documents.
     - Status: **PASS**.

6. **Automated Test Suites**:
   - `pytest tests/test_layout_analysis.py tests/test_table_reconstruction.py -v`: **30 passed in 22.42s** (**PASS**).
   - `python test_invoice_ocr.py`: **55 passed, 0 failed in 0.05s** (**PASS**).
   - Full regression test suite (194 tests): **194 passed in 91.65s** (**PASS**).

7. **Source Dataset Immutability**:
   - Verified `find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"`: **0 files modified**.
   - Direct `stat -f "%m %Sm %N"` check confirms all files retain original August 31, 2026 timestamps (**PASS**).

---

## 5-Component Handoff Protocol

### 1. Observation
- `invoice_ocr.py` lines 3454–3466:
  ```python
  all_tokens: list[OcrToken] = []

  for page in pages:
      norm_page, transform = normalize_page_geometry(page)
      normalized_pages.append(norm_page)
      page_transforms.append(transform)

      variants = generate_preprocessing_variants(norm_page.image)
      page_tokens = run_multiple_ocr_passes(variants)
      for tok in page_tokens:
          tok.page_number = norm_page.page_number
          tok.is_low_confidence = (tok.conf < MIN_CONFIDENCE)
  embedded_pdf_tokens: list[OcrToken] = []
  ```
  Notice that `all_tokens.extend(page_tokens)` is absent.
- Executing `process_invoice("/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf")`:
  ```
  OCR produced no tokens
  Invoice number: None
  Line items: 0
  Validation errors: ['OCR_NO_TOKENS']
  ```
- Executing `process_invoice` on a synthetic binarized image:
  ```
  OCR produced no tokens
  Validation errors: ['OCR_NO_TOKENS']
  Tokens count: 0
  ```
- Running OCR on Metro page 1 directly via `run_multiple_ocr_passes(variants)` produces **608 tokens** and **61 lines** including `НЕТРО БЪЛГАРИЯ Е0`, `ЕЙК: 121044756`.

### 2. Logic Chain
1. `all_tokens` is initialized to `[]` at line 3454.
2. In the page iteration loop, `page_tokens = run_multiple_ocr_passes(variants)` computes tokens for each page, but the tokens are never added to `all_tokens`.
3. If the input file is an image or a scanned PDF without an embedded text layer (or with $< 3$ statutory keywords), `embedded_pdf_tokens` remains `[]`.
4. As a result, `all_tokens` remains `[]`.
5. Line 3504 checks `if not all_tokens:`, logs `OCR produced no tokens`, appends `OCR_NO_TOKENS`, and returns an empty invoice.
6. Even on digital PDFs where `embedded_pdf_tokens` is populated, line 3500 `all_tokens = fuse_ocr_passes(embedded_pdf_tokens, all_tokens)` is called with `all_tokens == []`, so no Tesseract OCR tokens are fused.
7. This directly contradicts the requirement for graceful fallback on scanned PDFs and falsifies the handoff claim that OCR capability was preserved for Metro.

### 3. Caveats
- The unit tests in `tests/test_layout_analysis.py` and `tests/test_table_reconstruction.py` pass because they test isolated components with mock tokens, or test digital PDFs (`капина-01`, `капина-02`, `капина-03`) which have embedded digital text layers.
- Once `all_tokens.extend(page_tokens)` is restored, full end-to-end processing succeeds on both scanned PDFs (Metro) and digital PDFs (Kapina).

### 4. Conclusion
Milestone 3 implementations for coordinate token grouping, 7-zone layout, party orientation resolution, table column taxonomy, multi-line header sliding window, continuation row merging, and strict null fallback are well-engineered and meet all requirements.
However, due to the critical defect in `process_invoice` where `all_tokens.extend(page_tokens)` was omitted, all scanned invoices and images fail completely, and OCR fusion on digital invoices is bypassed.
The verdict is **REQUEST_CHANGES** until this single line is restored.

### 5. Verification Method
1. Verify the defect by running:
   ```bash
   .venv/bin/python -c '
   import invoice_ocr as iocr
   inv = iocr.process_invoice("/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf")
   print("Errors:", [e.code for e in inv.validation.errors])
   assert len(inv.line_items) > 0, "FAILED: Scanned PDF produced 0 items!"
   '
   ```
2. Verify the fix: Add `all_tokens.extend(page_tokens)` to line 3465 of `invoice_ocr.py`. Re-run the command above; it will extract 42 line items and 855 tokens.
3. Re-run test suites:
   ```bash
   .venv/bin/pytest tests/test_layout_analysis.py tests/test_table_reconstruction.py -v
   .venv/bin/python test_invoice_ocr.py
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```

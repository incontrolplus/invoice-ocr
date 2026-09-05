# Milestone 1: Multi-Format Ingestion (Iteration 2 Remediation) — Handoff Report

**Agent**: Worker (`teamwork_preview_worker_m1_iter2`)  
**Roles**: implementer, qa, specialist  
**Target Milestone**: Milestone 1: Multi-Format Ingestion (Iteration 2 Remediation)  
**Date**: 2026-09-05T00:40:00+03:00 (2026-09-04T21:40:00Z)  
**Working Directory**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m1_iter2`  
**Authoritative Requirements**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md` (R1)  
**Project Plan**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md` (M1)  
**Status**: **COMPLETE / READY FOR REVIEW & AUDIT**

---

## 1. Observation

### 1.1 Initial Baseline and Defect Confirmation
Prior to code remediation, executing the adversarial test suite:
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v
```
Resulted in **2 unhandled crash failures** (Exit code: 1):
1. Corrupted Page Tree Count Mismatch:
   ```
   FAILED tests/test_adversarial_ingestion.py::TestAdversarialUngracefulCrashBugs::test_corrupted_page_tree_count_mismatch_must_raise_clean_value_error
   E   IndexError: page 1 not in document
   ```
2. Overly Large MediaBox:
   ```
   FAILED tests/test_adversarial_ingestion.py::TestAdversarialUngracefulCrashBugs::test_extreme_mediabox_must_raise_clean_value_error
   E   pymupdf.mupdf.FzErrorLimit: code=5: Overly large image
   ```

Inspection of `invoice_ocr.py`:
- In `rasterize_pdf` (lines 655–693), `total_pages = len(doc)` was followed by a page iteration loop (`doc[idx]`, `page.get_pixmap()`) enclosed solely in `try ... finally: doc.close()`, with no `except` handler to translate renderer crashes into the specified `ValueError`. Furthermore, `dpi` was not validated against non-positive numbers.
- In `pixmap_to_bgr` (lines 640–653), `elif pix.n == 4:` unconditionally invoked `cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)` without verifying colorspace, causing CMYK Pixmaps (`n=4`, `DeviceCMYK`) to be treated as RGBA.
- In `LogicalLine` (lines 218–226), the field order was declared as `tokens, page_number, y_center, text, bbox`, diverging from the `PROJECT.md` interface contract line 99 (`tokens, bbox, text, page_number, y_center`).

### 1.2 Remediations Implemented
1. **`invoice_ocr.py` — `fitz` alias**:
   Defined `fitz = pymupdf` under the import block (lines 52–57), enabling `fitz.Pixmap` usage without deprecation warnings.
2. **`invoice_ocr.py` — `LogicalLine` Interface Contract**:
   Updated field declaration order to:
   ```python
   @dataclass
   class LogicalLine:
       tokens: list[OcrToken]
       bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
       text: str = ""
       page_number: int = 1
       y_center: float = 0.0
   ```
   Enhanced `__post_init__` to compute `bbox` (if `(0, 0, 0, 0)`), `text` (if empty), `y_center` (if `0.0`), and `page_number` (if default `1`) from tokens while safely preserving explicitly passed values.
3. **`invoice_ocr.py` — `pixmap_to_bgr` CMYK Conversion**:
   Added explicit check before `pix.n == 4`:
   ```python
   if pix.colorspace and pix.colorspace.name == "DeviceCMYK":
       rgb_pix = fitz.Pixmap(fitz.csRGB, pix)
       arr = np.frombuffer(rgb_pix.samples, dtype=np.uint8).reshape((rgb_pix.height, rgb_pix.width, 3))
       return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
   ```
4. **`invoice_ocr.py` — `rasterize_pdf` Validation & Graceful Exception Handling**:
   - Added validation at function start:
     ```python
     if dpi <= 0:
         raise ValueError(f"Invalid rasterization DPI: {dpi}. Must be a positive integer.")
     ```
   - Wrapped the page retrieval and rasterization loop in a dedicated exception handler:
     ```python
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
     ```
   - Enclosed all document handling in `finally: doc.close()` ensuring file descriptors are always freed.
5. **`tests/test_ingestion.py` — Regression Hardening**:
   - Augmented `test_dpi_scaling_proportionality` to assert `ValueError` on `dpi=0` and `dpi=-50`.
   - Augmented `test_pixmap_to_bgr_contiguity` to test CMYK Pixmap conversion to C-contiguous BGR ndarrays.
   - Augmented `test_logical_line_and_grouping_page_separation` to verify `LogicalLine` interface contract (positional and default instantiation).
   - Preserved exact suite count: 15 tests.

### 1.3 Empirical Verification Results
All four verification commands succeeded with 100% pass rates:

1. **Adversarial Ingestion Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v
   ```
   **Output**: `======================== 21 passed, 5 warnings in 1.85s ========================` (Exit code: 0)

2. **Ingestion Unit Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v
   ```
   **Output**: `======================== 15 passed, 5 warnings in 1.19s ========================` (Exit code: 0)

3. **Existing Parsing & Normalization Regression Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   **Output**: `TOTAL: 55 passed, 0 failed` (Exit code: 0)

4. **Live Acceptance PDF Run**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
   ```
   **Output**: Exit code 0, diagnostic logs on stderr, clean JSON document on stdout.

### 1.4 Strict Read-Only Dataset Immutability
Checked `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026`:
- `капина-01.pdf`: 7,516,207 bytes, mtime `Aug 31 23:55`
- `капина-02.pdf`: 8,148,645 bytes, mtime `Aug 31 23:56`
- `капина-03.pdf`: 7,218,503 bytes, mtime `Aug 31 23:58`
`find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"` returned **0 files**.

---

## 2. Logic Chain

1. **Contract Requirements**:
   - `ORIGINAL_REQUEST.md` (R1) & `PROJECT.md` (M1) require `load_document` to ingest PDF and image formats, never crash ungracefully, and raise clean `ValueError` on corrupted or invalid inputs.
   - `PROJECT.md` Interface Contract specifies `LogicalLine(tokens, bbox, text, page_number, y_center)`.
2. **Root Cause Analysis**:
   - In `rasterize_pdf`, PyMuPDF raises `IndexError` when page counts in the PDF dictionary differ from physical page objects, and `pymupdf.mupdf.FzErrorLimit` when MediaBox dimensions exceed rendering limits.
   - Because the inner page retrieval loop lacked an exception handler, these native errors escaped `load_document`, crashing callers ungracefully.
   - For `LogicalLine`, parameter positional order in the class definition swapped `page_number` with `bbox`, violating contract expectations for positional callers.
   - For `pixmap_to_bgr`, evaluating `pix.n == 4` before checking colorspace mistook 4-channel CMYK for 4-channel RGBA.
3. **Remediation Correctness**:
   - Catching `Exception as exc` during page retrieval and pixmap generation in `rasterize_pdf` and re-raising `ValueError(f"Failed to rasterize PDF document: {path} ({exc})") from exc` ensures all MuPDF and indexing failures are converted into contract-compliant `ValueError` exceptions.
   - Validating `dpi <= 0` at entry prevents invalid geometry matrices or arithmetic exceptions during rendering.
   - Re-ordering `LogicalLine` fields to `(tokens, bbox, text, page_number, y_center)` perfectly aligns with `PROJECT.md`, while `__post_init__` ensures default auto-derivation when `bbox`, `text`, or `y_center` are not explicitly provided.
   - Adding `pix.colorspace and pix.colorspace.name == "DeviceCMYK"` converts CMYK pixmaps to RGB before channel reordering to BGR.
4. **Verification Deductions**:
   - The adversarial test suite directly exercises count mismatch (`test_corrupted_page_tree_count_mismatch_must_raise_clean_value_error`) and extreme media boxes (`test_extreme_mediabox_must_raise_clean_value_error`). Both tests now pass cleanly with exit code 0.
   - The unit test suite confirms DPI validation, CMYK conversion, and LogicalLine interface contract behavior without regressions.

---

## 3. Caveats

1. **PyMuPDF Warning Annotations**:
   - In Python 3.14, SWIG-generated C extensions emit minor deprecation warnings regarding module attributes (`DeprecationWarning: builtin type swigvarlink has no __module__ attribute`). These are harmless internal upstream warnings from PyMuPDF 1.28.2 and do not impact functionality.
2. **Downstream Pipeline Milestones**:
   - The live invoice run outputs validation errors (`MISSING_SUPPLIER_NAME`, `TOTAL_SUM_MISMATCH`). These are expected outputs of the baseline OCR heuristics prior to Milestone 3 (Table Reconstruction) and Milestone 5 (Validation & Error Correction) and are not regressions in Milestone 1.

---

## 4. Conclusion

All defects and contract discrepancies identified by Challenger 1 and Reviewer 1 have been remediated:
- Robust graceful exception handling in `rasterize_pdf` with zero unhandled crash vulnerabilities.
- Strict positive DPI validation.
- Accurate CMYK Pixmap conversion in `pixmap_to_bgr`.
- Exact interface contract alignment for `LogicalLine`.
- 100% pass rate across adversarial tests (21/21), ingestion tests (15/15), regression tests (55/55), and live PDF execution (Exit code 0).
- Zero-touch immutability of the acceptance dataset verified.

---

## 5. Verification Method

To independently verify the implementation:

1. **Adversarial Ingestion Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
       /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_adversarial_ingestion.py -v
   ```
   *Expected*: 21 passed, 0 failed.

2. **Ingestion Unit Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
       /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_ingestion.py -v
   ```
   *Expected*: 15 passed, 0 failed.

3. **Regression Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python \
       /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/test_invoice_ocr.py
   ```
   *Expected*: 55 passed, 0 failed.

4. **Acceptance PDF Document Execution**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python \
       /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py \
       "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
   ```
   *Expected*: Exit code 0, diagnostic logging on stderr, valid JSON on stdout.

5. **Dataset Immutability Confirmation**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Expected*: Empty output (0 files).

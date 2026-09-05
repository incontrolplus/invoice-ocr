# Milestone 1: Multi-Format Ingestion — Independent Review & Adversarial Critic Report

**Reviewer**: Reviewer 1 (`teamwork_preview_reviewer_m1_1`)  
**Roles**: Reviewer (Objective Code Review) & Adversarial Critic (Stress-Testing & Edge-Case Mining)  
**Target Milestone**: Milestone 1 (Multi-Format Ingestion)  
**Date**: 2026-09-05T00:35:00+03:00 (2026-09-04T21:35:00Z)  
**Working Directory**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_reviewer_m1_1`  
**Authoritative Requirements**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md` (R1)  
**Project Plan**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md` (M1, Features 1–5, Interface Contracts lines 78–113)  
**Worker Handoff**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m1/handoff.md`  

**Verdict**: **REQUEST_CHANGES**  
**Overall Risk Assessment**: **MEDIUM-HIGH** (2 unhandled crash vulnerabilities under corrupted PDF structures, 1 dataclass signature contract drift)

---

## 1. Observation

### 1.1 Direct Inspection of Implementation Code (`invoice_ocr.py`)
1. **PyMuPDF Import & Constants (`lines 52–70`)**:
   ```python
   try:
       import pymupdf
   except ImportError:
       import fitz as pymupdf

   PDF_EXTENSIONS: set[str] = {".pdf"}
   IMAGE_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg"}
   SUPPORTED_EXTENSIONS: set[str] = {".pdf", ".png", ".jpg", ".jpeg"}
   DEFAULT_RASTER_DPI: int = 300
   MIN_CONFIDENCE: int = 60
   ```
   - Standardizes on `pymupdf` with `fitz` fallback, eliminating PyMuPDF 1.28.2 deprecation warnings.
   - Restricts formats strictly to `.pdf`, `.png`, `.jpg`, `.jpeg` per R1.

2. **Data Models (`lines 128–285`)**:
   - `PageImage(page_number: int, image: np.ndarray, width: int, height: int)` conforms directly to `PROJECT.md` line 88.
   - `OcrToken`: standard `bbox: tuple[int, int, int, int]`, `page_number: int = 1`, and `is_low_confidence: bool = False`. Retains properties (`left`, `top`, `width`, `height`, `right`, `bottom`, `center_x`, `center_y`) preserving 100% backward compatibility for existing algorithms.
   - `LogicalLine`: defined with fields `tokens: list[OcrToken], page_number: int = 1, y_center: float = 0.0, text: str = "", bbox: tuple[int, int, int, int] = (0, 0, 0, 0)`.
     - *Observation*: `PROJECT.md` defines `LogicalLine(tokens, bbox, text, page_number, y_center)`. In `invoice_ocr.py`, `page_number` is the 2nd positional argument, whereas in `PROJECT.md` `bbox` is the 2nd positional argument.
   - `TableRegion`: carries `page_number: int = 1` set from `line.page_number`.

3. **Ingestion Functions (`lines 635–754`)**:
   - `pixmap_to_bgr(pix: pymupdf.Pixmap) -> np.ndarray`: handles Grayscale (`n=1`), RGB (`n=3`), RGBA (`n=4`), and fallback via `pymupdf.Pixmap(pymupdf.csRGB, pix)`. Returns C-contiguous ndarrays via `cv2.cvtColor`.
     - *Observation*: In `pixmap_to_bgr`, `elif pix.n == 4:` blindly calls `cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)`. If a standalone CMYK Pixmap (`pix.n == 4`, `colorspace.name == "DeviceCMYK"`) is passed, it is misinterpreted as RGBA.
   - `rasterize_pdf(path: Path | str, dpi: int = DEFAULT_RASTER_DPI) -> list[PageImage]`:
     - Document opening (`lines 662–667`) is enclosed in `try ... except (EmptyFileError, FileDataError) ... except Exception` re-raising clean `ValueError`.
     - Checks `doc.is_encrypted and doc.needs_pass` and `len(doc) == 0`.
     - Guaranteed `finally: doc.close()`.
     - *Observation*: The rasterization loop (`lines 678–690`):
       ```python
       for idx in range(total_pages):
           page = doc[idx]
           pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
           bgr = pixmap_to_bgr(pix)
           ...
       ```
       contains **NO `except` block**. Only `finally: doc.close()` is present.
   - `load_image_page(path: Path | str) -> PageImage`: uses `Path.read_bytes()` + `cv2.imdecode(arr, cv2.IMREAD_COLOR)` ensuring safe decoding of Cyrillic file paths.
   - `load_document(path: Path | str, dpi: int = DEFAULT_RASTER_DPI) -> list[PageImage]`: verifies file existence (`FileNotFoundError`), checks extension in `SUPPORTED_EXTENSIONS`, dispatches to `rasterize_pdf` or `load_image_page`.

4. **Multi-Page Layout Partitioning (`lines 1072–1143`, `2356–2364`)**:
   - `group_tokens_into_lines`: partitions by `t.page_number` in `tokens_by_page = defaultdict(list)` before spatial sorting.
   - `group_lines_into_blocks`: partitions by `line.page_number` in `lines_by_page = defaultdict(list)`.
   - `process_invoice`: iterates over all pages from `load_document(path)`, generates multi-variant OCR passes per page, tags tokens with `tok.page_number = page.page_number` and `tok.is_low_confidence = (tok.conf < MIN_CONFIDENCE)`.

---

### 1.2 Independent Test Suite Execution Results
1. **Pytest Ingestion Suite (`tests/test_ingestion.py`)**:
   - Command: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v`
   - Result: `======================== 15 passed, 5 warnings in 1.20s ========================` (Exit code: 0)
   - Verified:
     - `test_single_page_pdf_ingestion`: PASSED
     - `test_multi_page_pdf_ingestion`: PASSED
     - `test_image_files_png_and_jpg`: PASSED
     - `test_unsupported_file_types_raise_clean_value_error`: PASSED
     - `test_missing_files_raise_file_not_found_error`: PASSED
     - `test_corrupted_pdf_and_image_raise_value_error`: PASSED
     - `test_password_protected_pdf_raises_value_error`: PASSED
     - `test_read_only_guarantee_on_source_files`: PASSED
     - `test_dpi_scaling_proportionality`: PASSED
     - `test_accepts_both_str_and_path_inputs`: PASSED
     - `test_load_image_backward_compatibility`: PASSED
     - `test_pixmap_to_bgr_contiguity`: PASSED
     - `test_ocr_token_bbox_and_properties`: PASSED
     - `test_logical_line_and_grouping_page_separation`: PASSED
     - `test_acceptance_kapina_pdf_read_only_ingestion`: PASSED

2. **Existing Unit Test Regression Suite (`test_invoice_ocr.py`)**:
   - Command: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py`
   - Result: `TOTAL: 55 passed, 0 failed` across `parse_money`, `normalize_eik`, `normalize_vat_number`, `parse_date`, `clean_ocr_artifacts`, `normalize_iban`. (Exit code: 0)

3. **Execution on Mandatory Acceptance PDF (`капина-01.pdf`)**:
   - Command: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"`
   - Result: Exit code 0.
   - Stderr: Diagnostic log `INFO: Rasterized PDF: капина-01.pdf (1 page(s) at 300 DPI)`, followed by preprocessing deskewing and OCR pass scoring logs.
   - Stdout: Clean JSON document containing `invoice_metadata`, `supplier`, `recipient`, `line_items`, `financial_summary`, `payment_details`, and `validation`.

4. **Tier 1 Feature Tests for Ingestion (`tests/e2e/test_tier1_features.py -k test_r1`)**:
   - Command: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/e2e/test_tier1_features.py -k "test_r1" -v`
   - Result: `5 passed, 26 deselected, 5 warnings in 1.18s` (Exit code: 0).

---

### 1.3 Adversarial Stress-Test Verification Results
We independently executed the adversarial stress test suite in `tests/test_adversarial_ingestion.py`:
- Command: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v`
- Result: **2 failed, 19 passed, 5 warnings in 5.10s** (Exit code: 1).

**Verbatim Failure 1 — Corrupted Page Tree (`IndexError`)**:
```
_ TestAdversarialUngracefulCrashBugs.test_corrupted_page_tree_count_mismatch_must_raise_clean_value_error _
    page = doc[idx]
  File ".venv/lib/python3.14/site-packages/pymupdf/__init__.py", line 2897, in __getitem__
    raise IndexError(f"page {i} not in document")
E   IndexError: page 1 not in document
```

**Verbatim Failure 2 — Extreme MediaBox (`FzErrorLimit`)**:
```
_ TestAdversarialUngracefulCrashBugs.test_extreme_mediabox_must_raise_clean_value_error _
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    ...
    return _mupdf.fz_new_pixmap_with_bbox(colorspace, bbox, seps, alpha)
E   pymupdf.mupdf.FzErrorLimit: code=5: Overly large image
```

---

### 1.4 Strict Read-Only Dataset Immutability Verification
We verified that zero files in `/Volumes/NO NAME/_ФАКТУРИ` were modified, created, or deleted:
1. `ls -la "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026"`:
   ```
   -rwx------ 1 diokarabaz staff 7516207 Aug 31 23:55 капина-01.pdf
   -rwx------ 1 diokarabaz staff 8148645 Aug 31 23:56 капина-02.pdf
   -rwx------ 1 diokarabaz staff 7218503 Aug 31 23:58 капина-03.pdf
   ```
2. Recursive filesystem modification sweep:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Output*: Exactly **empty** (0 files modified since Sep 4).

---

### 1.5 Integrity Audit
- **Hardcoding Check**: Grep queries for invoice numbers (`1100124585`), monetary totals (`161.12`), vendor names (`капина`, `kapina`) in `invoice_ocr.py` returned 0 occurrences.
- **Facade/Dummy Implementation Check**: Real rasterization, real OpenCV image decoding, real geometry clustering, and genuine OCR execution are present.
- **Shortcuts / Delegations**: Code was built natively using PyMuPDF, OpenCV, and Tesseract.
- **Fabricated Outputs Check**: All outputs in worker handoff were independently reproduced and matched byte-for-byte.
- **Finding**: **Zero integrity violations detected.** Work is authentic.

---

## 2. Logic Chain

1. **Contract Requirement**:
   - `load_document` docstring states: *"Raises: FileNotFoundError: If the file does not exist. ValueError: If the file format is unsupported or corrupted."*
   - Downstream batch and API pipelines rely on `load_document` raising clean `ValueError` on corrupt files rather than crashing the Python process with unexpected exceptions.
2. **Missing Exception Handling in `rasterize_pdf`**:
   - While `doc = pymupdf.open(str(path))` is enclosed in `try ... except`, the inner page loop (`lines 678–690`) has no `except` handler, only `finally: doc.close()`.
   - When a PDF with an internal page catalog mismatch (`/Count 2` but 1 page) is parsed, `len(doc)` returns 2, but `doc[1]` raises `IndexError`.
   - When a PDF with an extreme MediaBox (`[0 0 10000000 10000000]`) is rasterized, `get_pixmap()` raises `pymupdf.mupdf.FzErrorLimit`.
   - Both exceptions escape `load_document` unhandled, violating the graceful degradation contract.
3. **Interface Contract Drift on `LogicalLine`**:
   - `PROJECT.md` line 99 specifies: `LogicalLine(tokens: list[OcrToken], bbox: tuple[int, int, int, int], text: str, page_number: int, y_center: float)`.
   - In `invoice_ocr.py`, `page_number` is the 2nd field and `bbox` is the 5th field.
   - Positional instantiation `LogicalLine(tokens, bbox, text, page_number, y_center)` silently places the `bbox` tuple into `page_number`, causing type corruption.
4. **CMYK Misinterpretation in `pixmap_to_bgr`**:
   - A standalone CMYK Pixmap has `pix.n == 4`. `pixmap_to_bgr` assumes any `pix.n == 4` is RGBA and converts it via `cv2.COLOR_RGBA2BGR`, which distorts CMYK colors. While `rasterize_pdf` requests `csRGB`, standalone utility calls on CMYK pixmaps fail color accuracy.
5. **Conclusion**:
   - Because the pipeline fails 2 adversarial tests with unhandled crashes and exhibits contract parameter drift, the review verdict must be **REQUEST_CHANGES**.

---

## 3. Findings

### [Critical] Finding 1: Unhandled Exceptions during PDF Page Retrieval and Rasterization
- **What**: Corrupted PDFs with page tree count mismatches or extreme MediaBoxes raise unhandled `IndexError` and `pymupdf.mupdf.FzErrorLimit`.
- **Where**: `invoice_ocr.py`, lines 669–693 in `rasterize_pdf`.
- **Why**: Violates the `load_document` interface contract (`Raises: ValueError if corrupted`). Crashes batch processing.
- **Suggestion**:
  Wrap the inner rasterization block in:
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

### [Major] Finding 2: `LogicalLine` Dataclass Parameter Order Divergence
- **What**: The field declaration order in `LogicalLine` puts `page_number` 2nd, `y_center` 3rd, `text` 4th, and `bbox` 5th.
- **Where**: `invoice_ocr.py`, lines 219–226.
- **Why**: Diverges from `PROJECT.md` contract line 99 (`tokens, bbox, text, page_number, y_center`). Positional instantiations swap `page_number` with `bbox`.
- **Suggestion**:
  Update `LogicalLine` dataclass field order to match `PROJECT.md` while keeping sensible defaults:
  ```python
  @dataclass
  class LogicalLine:
      tokens: list[OcrToken]
      bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
      text: str = ""
      page_number: int = 1
      y_center: float = 0.0
  ```

### [Minor] Finding 3: Missing Validation for Non-Positive DPI
- **What**: `rasterize_pdf(path, dpi)` does not validate that `dpi > 0`.
- **Where**: `invoice_ocr.py`, line 655 in `rasterize_pdf`.
- **Why**: Passing `dpi=0` or `dpi=-1` can cause unexpected matrix calculation or division-by-zero errors in rendering backends.
- **Suggestion**:
  Add validation at the start of `rasterize_pdf`:
  ```python
  if dpi <= 0:
      raise ValueError(f"Invalid rasterization DPI: {dpi}. Must be a positive integer.")
  ```

### [Minor] Finding 4: CMYK Detection in `pixmap_to_bgr`
- **What**: `pix.n == 4` is assumed to always be RGBA.
- **Where**: `invoice_ocr.py`, lines 646–648 in `pixmap_to_bgr`.
- **Why**: CMYK Pixmaps also have `n = 4` (`pymupdf.csCMYK.n == 4`), but their channel layout is Cyan, Magenta, Yellow, Black rather than Red, Green, Blue, Alpha.
- **Suggestion**:
  Check `pix.alpha` or `pix.colorspace`:
  ```python
  if pix.n == 1:
      ...
  elif pix.n == 3:
      ...
  elif pix.n == 4 and pix.alpha:
      arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 4))
      return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
  else:
      rgb_pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
      arr = np.frombuffer(rgb_pix.samples, dtype=np.uint8).reshape((rgb_pix.height, rgb_pix.width, 3))
      return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
  ```

---

## 4. Caveats

1. **Later Milestone E2E Failures are Expected**:
   - The 7 failures observed during the broad `pytest tests/` run (e.g. `test_r3_03_table_header_synonym_detection`, `test_real_01_kapina_01_acceptance` expecting 14 line items) belong to Milestone 3 (Table Reconstruction) and Milestone 5 (Financial Validation). They do not represent regressions in Milestone 1.
2. **Memory Footprint for Massive PDFs**:
   - Rasterizing a 50-page document at 300 DPI will allocate ~1.3 GB of uncompressed uint8 numpy arrays in memory. For current Bulgarian invoices (1–3 pages), the working set (~25–75 MB) is completely safe. A lazy page generator may be considered for batch processing in Milestone 6.

---

## 5. Conclusion & Actionable Next Steps

**Verdict**: **REQUEST_CHANGES**

Milestone 1 successfully implements multi-format ingestion (`.pdf`, `.png`, `.jpg`, `.jpeg`), multi-page token coordinate tracking with `page_number` and `bbox`, backward compatibility, and zero-touch dataset protection. However, it must be hardened against corrupted PDF page trees and extreme media boxes before approval.

### Action Items for Worker (`teamwork_preview_worker_m1`):
1. **Wrap page rasterization loop in `rasterize_pdf`** to catch all rendering/structure exceptions and re-raise them as clean `ValueError` with path context.
2. **Add DPI validation** (`dpi > 0`) at entry of `rasterize_pdf`.
3. **Re-align `LogicalLine` dataclass field order** to match `PROJECT.md` interface contract (`tokens, bbox, text, page_number, y_center`).
4. **Refine `pixmap_to_bgr`** to safely handle CMYK pixmaps (`pix.n == 4 and not pix.alpha`).
5. **Verify that all 21 tests in `tests/test_adversarial_ingestion.py` pass** alongside all 15 tests in `tests/test_ingestion.py` and 55 tests in `test_invoice_ocr.py`.

---

## 6. Verification Method

To independently verify the resolution of these findings:

### 6.1 Execute Adversarial Ingestion Test Suite
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
    /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_adversarial_ingestion.py -v
```
- Current result: 2 failed (`test_corrupted_page_tree_count_mismatch_must_raise_clean_value_error`, `test_extreme_mediabox_must_raise_clean_value_error`), 19 passed.
- Required result after fix: **21 passed, 0 failed**.

### 6.2 Execute Ingestion Unit Tests
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
    /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_ingestion.py -v
```
- Required result: **15 passed, 0 failed**.

### 6.3 Execute Regression Tests
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python \
    /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/test_invoice_ocr.py
```
- Required result: `TOTAL: 55 passed, 0 failed`.

### 6.4 Execute Acceptance Document
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python \
    /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py \
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
```
- Required result: Exit code 0, clean JSON to stdout, diagnostic logging to stderr.

### 6.5 Confirm Zero-Touch Invariance
```bash
find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
```
- Required result: 0 files returned.

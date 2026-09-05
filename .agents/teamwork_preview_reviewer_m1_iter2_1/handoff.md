# Milestone 1: Multi-Format Ingestion (Iteration 2) — Review & Adversarial Audit Report

**Agent**: Reviewer 1 (`teamwork_preview_reviewer_m1_iter2_1`)  
**Roles**: reviewer, critic  
**Target Milestone**: Milestone 1: Multi-Format Ingestion (Iteration 2 Remediation)  
**Date**: 2026-09-05T00:44:00+03:00 (2026-09-04T21:44:00Z)  
**Working Directory**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_reviewer_m1_iter2_1`  
**Verdict**: **APPROVE**

---

## 1. Observation

Direct inspection of files, line numbers, and tool execution results:

### 1.1 `invoice_ocr.py` Code Inspections
1. **`rasterize_pdf` Exception Handling and File Closure (`invoice_ocr.py:664-711`)**:
   - Lines 671–672:
     ```python
     if dpi <= 0:
         raise ValueError(f"Invalid rasterization DPI: {dpi}. Must be a positive integer.")
     ```
   - Lines 674–680:
     ```python
     try:
         doc = pymupdf.open(str(path))
     except (pymupdf.EmptyFileError, pymupdf.FileDataError) as exc:
         raise ValueError(f"Failed to open PDF document: {path} ({exc})") from exc
     except Exception as exc:
         raise ValueError(f"Failed to open PDF document: {path} ({exc})") from exc
     ```
   - Lines 681–710:
     ```python
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
     ```
   Direct execution with mocked `doc[1]` raising `IndexError("page 1 not in document")` confirmed:
   - Output: `Caught clean ValueError: Failed to rasterize PDF document: fake.pdf (page 1 not in document)`
   - Confirmed: `doc.close()` was executed via `finally:`.

2. **DPI Validation (`invoice_ocr.py:671-672`)**:
   - Testing `rasterize_pdf('fake.pdf', dpi=0)` and `dpi=-500` cleanly raises `ValueError("Invalid rasterization DPI: ... Must be a positive integer.")`.

3. **`LogicalLine` Interface Contract (`invoice_ocr.py:220-243` vs `PROJECT.md:98-105`)**:
   - `PROJECT.md` line 98–105 specifies:
     ```python
     @dataclass
     class LogicalLine:
         tokens: list[OcrToken]
         bbox: tuple[int, int, int, int]
         text: str
         page_number: int
         y_center: float
     ```
   - `invoice_ocr.py` lines 220–228 declares:
     ```python
     @dataclass
     class LogicalLine:
         """A group of tokens sharing approximately the same Y coordinate on a specific page."""
         tokens: list[OcrToken]
         bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
         text: str = ""
         page_number: int = 1
         y_center: float = 0.0
     ```
   - Inspected with `[f.name for f in dataclasses.fields(LogicalLine)]`:
     Output: `['tokens', 'bbox', 'text', 'page_number', 'y_center']`.
   - Positional instantiation `LogicalLine([t1], (100, 200, 50, 20), "Explicit Text", 1, 210.0)` verified.
   - Default instantiation `LogicalLine([t1])` auto-derives `bbox`, `text`, `page_number`, `y_center`.
   - Edge case `LogicalLine([])` with empty token list verified: initializes defaults without raising `ZeroDivisionError` or crashing.

4. **`pixmap_to_bgr` Color Space Robustness (`invoice_ocr.py:640-662`)**:
   - Verified that `pix.colorspace and pix.colorspace.name == "DeviceCMYK"` correctly converts CMYK pixmaps via `fitz.Pixmap(fitz.csRGB, pix)` before converting to BGR.
   - Tested DeviceGray (n=1), DeviceRGB (n=3), RGBA (n=4), DeviceCMYK (n=4): all produce C-contiguous BGR `np.ndarray` objects.

### 1.2 Test Execution Results
1. **Adversarial Ingestion Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v
   ```
   - **Result**: `21 passed, 5 warnings in 2.74s` (Exit code: 0).
   - Verifies: random byte streams, truncated headers, corrupted xref offsets, corrupted flate streams, disguised files (text, zip, binary executables), varying dimensions/orientations, file descriptor leaks, memory leaks, corrupted page tree count mismatch, and extreme mediabox dimensions.

2. **Ingestion Unit Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v
   ```
   - **Result**: `15 passed, 5 warnings in 1.25s` (Exit code: 0).
   - Verifies: single-page PDF, multi-page PDF, PNG/JPG loading, unsupported formats rejection, missing files (`FileNotFoundError`), corrupted files, password-protected PDF, DPI scaling and validation, `LogicalLine` interface contract, and read-only source immutability.

3. **Regression Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   - **Result**: `TOTAL: 55 passed, 0 failed` (Exit code: 0).

4. **Live PDF Pipeline Execution**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
   ```
   - **Result**: Exit code 0. Clean JSON output on stdout, logging on stderr.

5. **Challenger Test Suite Execution**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_challenger_m1_2.py -v
   ```
   - **Result**: `16 passed, 5 warnings in 44.08s` (Exit code: 0).
   - Verifies real PDFs from external volume, multi-page rasterization, coordinate accessors, confidence thresholding, block isolation, empty tokens safety, extreme coordinates, and memory leak stress testing.

6. **Strict Source Volume Immutability Audit**:
   - Executed: `find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"`
   - Output: 0 files.
   - Executed: `ls -la "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/"`
   - File listing:
     - `капина-01.pdf`: 7,516,207 bytes, mtime `Aug 31 23:55`
     - `капина-02.pdf`: 8,148,645 bytes, mtime `Aug 31 23:56`
     - `капина-03.pdf`: 7,218,503 bytes, mtime `Aug 31 23:58`
   - Verified zero modifications to `/Volumes/NO NAME/_ФАКТУРИ`.

---

## 2. Logic Chain

1. **Defect Remediation Verification**:
   - *Observation 1.1(1)* shows the page retrieval loop in `rasterize_pdf` is wrapped in `try: ... except ValueError: raise except Exception as exc: raise ValueError(...) from exc` with `finally: doc.close()`. This directly fixes the unhandled crashes (`IndexError` from corrupted page tree count mismatch and `FzErrorLimit` from extreme mediabox) previously reported by Challenger 1 and Reviewer 1.
   - *Observation 1.1(2)* confirms `dpi <= 0` is checked prior to document loading and raises a descriptive `ValueError`.
   - *Observation 1.1(3)* shows the dataclass field ordering of `LogicalLine` matches `PROJECT.md` line 99: `(tokens, bbox, text, page_number, y_center)`. Positional instantiation tests succeeded, while defaults preserve backward compatibility.
   - *Observation 1.1(4)* confirms CMYK pixmaps are detected and converted via RGB intermediate, avoiding channel corruption.
2. **Empirical Conformance**:
   - *Observations 1.2(1-5)* demonstrate that all unit, adversarial, regression, and challenger tests pass with 100% success rate across 107 test cases (21 + 15 + 55 + 16).
   - Live execution on `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf` completed with exit code 0.
3. **Integrity and Immutability Verification**:
   - *Observation 1.2(6)* proves the strict constraint was respected: no files on `/Volumes/NO NAME/_ФАКТУРИ` were created, modified, or deleted.
   - Code audit confirmed no hardcoded bypasses, dummy implementations, or shortcuts exist in `invoice_ocr.py`.

---

## 3. Caveats

1. **Downstream Milestone Failures**:
   - Full E2E tests (`tests/e2e/`) currently have 7 expected failures (82 passed, 7 failed) relating to Milestones 3 (Table Reconstruction), 4 (Field Extraction), and 5 (Validation & Error Correction). These are not regressions in Milestone 1.
2. **PyMuPDF SWIG Warnings**:
   - In Python 3.14, PyMuPDF emits `DeprecationWarning: builtin type swigvarlink has no __module__ attribute`. These are harmless upstream library warnings that do not affect runtime execution.

---

## 4. Conclusion

Milestone 1 Iteration 2 fully resolves all feedback and requirements:
1. `rasterize_pdf` catches all exceptions during page retrieval/rasterization and re-raises clean `ValueError`, with document closure guaranteed in `finally:`.
2. Non-positive DPI (`dpi <= 0`) raises clean `ValueError`.
3. `LogicalLine` interface contract strictly matches `PROJECT.md` line 99.
4. All 4 required verification commands passed:
   - `test_adversarial_ingestion.py`: 21/21 passed
   - `test_ingestion.py`: 15/15 passed
   - `test_invoice_ocr.py`: 55/55 passed
   - Acceptance PDF run: exit code 0
5. External dataset `/Volumes/NO NAME/_ФАКТУРИ` remains strictly untouched (0 modifications).
6. Zero integrity violations detected.

**Final Verdict**: **APPROVE**.

---

## 5. Verification Method

To independently verify this evaluation:

```bash
# 1. Verify adversarial ingestion tests (21/21)
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
  /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_adversarial_ingestion.py -v

# 2. Verify unit ingestion tests (15/15)
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
  /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_ingestion.py -v

# 3. Verify regression tests (55/55)
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python \
  /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/test_invoice_ocr.py

# 4. Verify live PDF run (exit code 0)
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python \
  /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py \
  "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"

# 5. Verify challenger tests (16/16)
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
  /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_challenger_m1_2.py -v

# 6. Verify zero modifications on source volume
find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
```

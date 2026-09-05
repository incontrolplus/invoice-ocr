# Milestone 1: Multi-Format Ingestion — Challenge Report & Handoff

**Agent**: Challenger 1 (`teamwork_preview_challenger_m1_1`)  
**Target Milestone**: Milestone 1 (Multi-Format Ingestion)  
**Target Specifications**: `ORIGINAL_REQUEST.md` (R1) & `PROJECT.md` (M1)  
**Worker Handoff**: `.agents/teamwork_preview_worker_m1/handoff.md`  
**Verdict**: **REQUEST_CHANGES**  
**Overall Risk Assessment**: **MEDIUM-HIGH**  

---

## 1. Observation

### 1.1 Unhandled Crash Vulnerabilities in `rasterize_pdf`
In `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py`, lines 669–693:
```python
    try:
        if doc.is_encrypted and doc.needs_pass:
            raise ValueError(f"Encrypted or password-protected PDF is not supported: {path}")

        total_pages = len(doc)
        if total_pages == 0:
            raise ValueError(f"PDF document contains 0 pages: {path}")

        pages: list[PageImage] = []
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
        logger.info("Rasterized PDF: %s (%d page(s) at %d DPI)", path.name, total_pages, dpi)
        return pages
    finally:
        doc.close()
```
While document opening (lines 662–667) catches exceptions and re-raises `ValueError`, the page rasterization loop (lines 678–690) contains **no `except` block**. Only `finally: doc.close()` is present.

### 1.2 Verbatim Empirical Failures from Adversarial Test Harness
We executed the adversarial test harness in `tests/test_adversarial_ingestion.py` using pytest:
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v
```
Output:
```
=================================== FAILURES ===================================
_ TestAdversarialUngracefulCrashBugs.test_corrupted_page_tree_count_mismatch_must_raise_clean_value_error _
    def __getitem__(self, i=0):
        ...
        if i not in self:
>           raise IndexError(f"page {i} not in document")
E           IndexError: page 1 not in document

.venv/lib/python3.14/site-packages/pymupdf/__init__.py:2897: IndexError

_ TestAdversarialUngracefulCrashBugs.test_extreme_mediabox_must_raise_clean_value_error _
>       return _mupdf.fz_new_pixmap_with_bbox(colorspace, bbox, seps, alpha)
E       pymupdf.mupdf.FzErrorLimit: code=5: Overly large image

.venv/lib/python3.14/site-packages/pymupdf/mupdf.py:52828: FzErrorLimit
=========================== short test summary info ============================
FAILED tests/test_adversarial_ingestion.py::TestAdversarialUngracefulCrashBugs::test_corrupted_page_tree_count_mismatch_must_raise_clean_value_error
FAILED tests/test_adversarial_ingestion.py::TestAdversarialUngracefulCrashBugs::test_extreme_mediabox_must_raise_clean_value_error
=================== 2 failed, 19 passed, 5 warnings in 3.13s ===================
```

### 1.3 Successful Stress Tests (19 of 21 Passed)
The pipeline successfully passed 19 adversarial tests:
1. **Corrupted byte streams**: Random noise files from 1 byte to 64 KB all raise clean `ValueError`.
2. **Truncated PDFs**: Missing trailer/xref and truncated headers raise clean `ValueError`.
3. **Corrupted startxref / damaged xref table**: PyMuPDF either repairs or raises `ValueError`.
4. **Corrupted FlateDecode streams**: PyMuPDF logs zlib warnings and avoids process crashes.
5. **Zero-byte files**: Zero-byte PDFs and images (`.png`, `.jpg`, `.jpeg`) raise clean `ValueError`.
6. **Disguised non-PDF files**: Plain text, ZIP archives, and ELF binaries disguised as `.pdf` raise clean `ValueError`.
7. **Disguised non-images**: Arbitrary binary disguised as `.png` or `.jpg` raises clean `ValueError`.
8. **Multi-page varied dimensions & rotations**: 5-page PDF with mixed aspect ratios (A4 portrait, A4 landscape, Letter 90°, Square 180°, Till roll 270°) rendered accurately with correct dimensions, 1-based page numbers, and C-contiguous arrays.
9. **File Descriptor Leak Test**: 400 repeated ingestion operations (100 valid PDFs, 100 valid PNGs, 100 corrupt files, 100 missing files) showed **0 FD leakage** (`/dev/fd` count remained 5).
10. **Memory Leak Test**: 80 repeated 4-page PDF rasterizations (320 pages total) stabilized after buffer allocation, with memory drift < 1 MB.

### 1.4 Source Dataset Immutability Verification
Inspection of `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026`:
- `капина-01.pdf`: 7,516,207 bytes, mtime `Aug 31 23:55` (unmodified).
- `капина-02.pdf`: 8,148,645 bytes, mtime `Aug 31 23:56` (unmodified).
- `капина-03.pdf`: 7,218,503 bytes, mtime `Aug 31 23:58` (unmodified).
- Exactly **0 files** modified, created, or deleted across the entire volume.

---

## 2. Logic Chain

1. **Contract Requirement**:
   - `load_document` contract docstring: *"Raises: FileNotFoundError: If the file does not exist. ValueError: If the file format is unsupported or corrupted."*
   - Dispatch Requirement 2: *"Verify that `load_document` never crashes ungracefully and raises clean `ValueError` or `FileNotFoundError`."*
2. **Missing Exception Handling in `rasterize_pdf`**:
   - In `invoice_ocr.py`, `doc = pymupdf.open(str(path))` is wrapped in `try...except`, but the loop iterating over pages `doc[idx]` and calling `page.get_pixmap()` is only enclosed in `try...finally: doc.close()`.
3. **Reproducer 1 — Corrupted Page Tree (`IndexError`)**:
   - A PDF whose catalog specifies `/Count 2` but whose page tree only contains 1 child causes `len(doc)` to return 2.
   - When the loop attempts `doc[1]`, PyMuPDF raises `IndexError("page 1 not in document")`.
   - Because `IndexError` is unhandled, `load_document` terminates ungracefully with an unhandled `IndexError`.
4. **Reproducer 2 — Overly Large MediaBox (`FzErrorLimit`)**:
   - A PDF containing an adversarial or corrupted MediaBox (e.g. `[0 0 10000000 10000000]`) causes MuPDF's renderer to raise `pymupdf.mupdf.FzErrorLimit("code=5: Overly large image")`.
   - Because `FzErrorLimit` is unhandled, `load_document` terminates ungracefully with an unhandled `FzErrorLimit`.
5. **Mitigation Proof**:
   - Wrapping the rasterization loop with:
     ```python
     except ValueError:
         raise
     except Exception as exc:
         raise ValueError(f"Failed to rasterize PDF document: {path} ({exc})") from exc
     ```
     empirically satisfies the contract and converts both unhandled crashes into clean `ValueError` while preserving `finally: doc.close()`.

---

## 3. Caveats

1. **PyMuPDF Inherent Text / Image Formats**:
   - PyMuPDF can natively open HTML and image files even when passed with a `.pdf` extension. This is benign behavior of the underlying MuPDF engine and does not cause pipeline crashes.
2. **Initial Memory Allocation**:
   - Rasterizing 4 pages at 300 DPI produces ~104 MB of uncompressed raw uint8 pixel arrays. Initial RSS increases by this working set size on first multi-page rasterization, but memory remains flat on subsequent iterations.

---

## 4. Conclusion & Required Changes

**Verdict**: **REQUEST_CHANGES**

The Milestone 1 ingestion pipeline is well-designed and satisfies format compatibility, multi-page coordinate tracking, and resource leak invariance. However, it fails the strict graceful error handling requirement on corrupted page structures and extreme media boxes.

### Required Changes for Worker:
1. In `invoice_ocr.py` within `rasterize_pdf`:
   - Catch all exceptions during the page retrieval and rasterization loop (`doc[idx]`, `page.get_pixmap()`, `pixmap_to_bgr()`) and raise a clean `ValueError(f"Failed to rasterize PDF document: {path} ({exc})") from exc`.
   - Validate that `dpi > 0`, raising `ValueError(f"Invalid rasterization DPI: {dpi}. Must be a positive integer.")` if non-positive.
2. Verify all 21 tests in `tests/test_adversarial_ingestion.py` pass alongside existing unit tests.

---

## 5. Verification Method

To reproduce findings and verify resolution:

### 5.1 Run Adversarial Stress Test Suite
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
    /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_adversarial_ingestion.py -v
```
Currently: 2 failed (`test_corrupted_page_tree_count_mismatch_must_raise_clean_value_error`, `test_extreme_mediabox_must_raise_clean_value_error`), 19 passed.  
Expected after fix: 21 passed.

### 5.2 Verify Regression Unit Tests Still Pass
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
    /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_ingestion.py \
    /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/test_invoice_ocr.py -v
```
Expected: 21 passed.

### 5.3 Verify Source Dataset Zero-Touch Invariance
```bash
ls -l "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026"
```
Expected: All files retain August 31 timestamps and original sizes.

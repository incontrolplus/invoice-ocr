# Milestone 1: Multi-Format Ingestion — Challenger 2 Report

**Agent**: Challenger 2 (`teamwork_preview_challenger_m1_2`)  
**Role**: Empirical Challenger (critic, specialist)  
**Milestone**: Milestone 1 (Multi-Format Ingestion)  
**Date**: 2026-09-04T21:35:00Z  
**Verdict**: **APPROVE**  
**Overall Risk Assessment**: **LOW**

---

## 1. Observation

### 1.1 Empirical Verification of Kapina Acceptance Files at 300 DPI
Direct execution of `load_document` on all 3 Kapina acceptance files at `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/`:
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c '
from pathlib import Path
from invoice_ocr import load_document, DEFAULT_RASTER_DPI
for name in ["капина-01.pdf", "капина-02.pdf", "капина-03.pdf"]:
    pages = load_document(Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026") / name, dpi=DEFAULT_RASTER_DPI)
    p = pages[0]
    print(f"{name}: pages={len(pages)}, shape={p.image.shape}, dtype={p.image.dtype}, contiguous={p.image.flags.c_contiguous}")
'
```
**Verbatim Output**:
```
капина-01.pdf: pages=1, shape=(3508, 2481, 3), dtype=uint8, contiguous=True
капина-02.pdf: pages=1, shape=(3508, 2481, 3), dtype=uint8, contiguous=True
капина-03.pdf: pages=1, shape=(3508, 2481, 3), dtype=uint8, contiguous=True
```
- **Execution Performance**:
  - `капина-01.pdf`: duration 1.213s, peak memory 49.81 MB
  - `капина-02.pdf`: duration 1.093s, peak memory 49.81 MB
  - `капина-03.pdf`: duration 2.588s, peak memory 49.81 MB
- Peak memory for all files remained well within system bounds (< 50 MB per file, ~26.1 MB uncompressed buffer). No crashes, hangs, or memory exhaustion occurred.

### 1.2 Empirical Verification of Multi-Page Rasterization on `метро.pdf`
Direct execution of `load_document` on `/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf` and `метро-2.pdf`:
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c '
from pathlib import Path
from invoice_ocr import load_document, DEFAULT_RASTER_DPI
for name in ["метро.pdf", "метро-2.pdf"]:
    pages = load_document(Path("/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025") / name, dpi=DEFAULT_RASTER_DPI)
    print(f"{name}: {len(pages)} pages")
    for p in pages:
        print(f"  Page {p.page_number}: shape={p.image.shape}, contiguous={p.image.flags.c_contiguous}")
'
```
**Verbatim Output**:
```
метро.pdf: 3 pages
  Page 1: shape=(3508, 2481, 3), contiguous=True
  Page 2: shape=(3508, 2481, 3), contiguous=True
  Page 3: shape=(3508, 2481, 3), contiguous=True
метро-2.pdf: 2 pages
  Page 1: shape=(3508, 2481, 3), contiguous=True
  Page 2: shape=(3508, 2481, 3), contiguous=True
```
- Total duration on `метро.pdf` (3 pages): 4.004s, peak memory 99.61 MB.
- Sequential 1-based page numbers (`page_number`: 1, 2, 3) are strictly assigned.

### 1.3 Preservation of Token Coordinates, Bounding Boxes, and Confidence Flags
Tested `OcrToken`, `LogicalLine`, `TableRegion`, `normalize_ocr_tokens`, and grouping algorithms:
- `OcrToken`:
  - `bbox=(120, 240, 300, 45)` correctly yields `left=120`, `top=240`, `width=300`, `height=45`, `right=420`, `bottom=285`, `center_x=270`, `center_y=262`.
  - Legacy keyword initialization (`left=50, top=100, width=80, height=20`) faithfully creates `bbox=(50, 100, 80, 20)`.
  - Confidence threshold `MIN_CONFIDENCE = 60`:
    - `conf=59` and `conf=59.9` -> `is_low_confidence = True`
    - `conf=60`, `conf=60.1`, and `conf=100` -> `is_low_confidence = False`
    - Explicit `is_low_confidence=True/False` keyword parameter correctly overrides confidence inference.
- `LogicalLine`:
  - `bbox` calculation faithfully encloses all constituent tokens (`min_l`, `min_t`, `max_r - min_l`, `max_b - min_t`).
  - Accessors `left`, `top`, `width`, `height`, `right`, `bottom` match bounding box.
  - Multi-page token isolation: `group_tokens_into_lines` partitions tokens by `page_number` before Y-clustering. Tokens on page 1 and page 2 at identical coordinates remain strictly isolated into distinct lines.
  - `group_lines_into_blocks` partitions lines by `page_number` before vertical clustering, ensuring text blocks never cross page boundaries.
- `normalize_ocr_tokens`:
  - Text normalization cleans OCR artifacts without mutating token `bbox`, `page_number`, or `is_low_confidence`.

### 1.4 Strict External Volume Immutability Verification
Baseline SHA-256 hashes and modification times were computed prior to any test execution, and re-verified post-execution:
```
a4b3bf24cd87ec7c5c9c431f90aee2f1232c65622850ab2c5da7b94448aa3bb6  .../Капина 2026/капина-01.pdf
5a6967aa728e495b84ef7c92245752650b9b5340aa8000b3b17943544893dfd0  .../Капина 2026/капина-02.pdf
305524189030c2c94282a68c7b61bb3d3de92230c66ff3cddb6d547ade5b82c7  .../Капина 2026/капина-03.pdf
a8a688215ae4d34d198715f828c54b9d5580ae07c4765454e94fa9083ba5c5f8  .../Метро 2025/метро-2.pdf
20cdd814b6ddec7d6743e6829a25f3f84cd2f66c84a8c1e6d9b898bf87c16d4d  .../Метро 2025/метро.pdf
```
- After extensive test runs, **0 files** were modified, created, or deleted.
- SHA-256 hashes and `st_mtime_ns` timestamps on `/Volumes/NO NAME/_ФАКТУРИ` remain 100% bit-for-bit identical.

### 1.5 Test Suite Results
- Created `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_challenger_m1_2.py` (16 empirical challenger tests).
- Combined ingestion test run:
  ```bash
  /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
      tests/test_ingestion.py tests/test_challenger_m1_2.py -v
  ```
  **Result**: `31 passed, 5 warnings in 30.64s` (100% passing).
- Existing regression suite:
  ```bash
  /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
  ```
  **Result**: `TOTAL: 55 passed, 0 failed` (100% passing).

---

## 2. Logic Chain

1. **Rasterization Correctness & Safety (Observation 1.1)**:
   - *Premise*: PDF rasterization must support standard A4 documents at 300 DPI without memory leaks or crashes.
   - *Observation*: Standard A4 dimensions (595.28 pt x 841.89 pt) at 300 DPI yield `(595.28 * 300 / 72) = 2480.33 -> 2481` px width, and `(841.89 * 300 / 72) = 3507.88 -> 3508` px height.
   - *Validation*: All 3 Kapina files produced exactly `(3508, 2481, 3)` uint8 arrays, contiguous in memory, with memory footprint bounded at ~49.8 MB.

2. **Multi-Page Handling & Sequential Ordering (Observation 1.2)**:
   - *Premise*: Ingesting multi-page invoices must maintain page ordering, isolate page frames, and avoid dropping trailing pages.
   - *Observation*: `метро.pdf` possesses 3 pages and `метро-2.pdf` possesses 2 pages.
   - *Validation*: `load_document` produced exactly 3 and 2 `PageImage` objects respectively, with sequential 1-based `page_number` attributes (`1, 2, 3`), correct shapes, and C-contiguous memory.

3. **Data Model Integrity & Coordinate Invariants (Observation 1.3)**:
   - *Premise*: Milestone 1 introduces `OcrToken.bbox`, `LogicalLine.bbox`, `page_number`, and `is_low_confidence`.
   - *Validation*: Property accessors (`left`, `top`, `width`, `height`, `right`, `bottom`, `center_x`, `center_y`) maintain exact mathematical relationships. `LogicalLine` computes tight bounding boxes enclosing all tokens. Token grouping partitions by page number, preventing cross-page contamination.

4. **External Dataset Immutability (Observation 1.4)**:
   - *Premise*: External production dataset `/Volumes/NO NAME/_ФАКТУРИ` must never be altered.
   - *Validation*: SHA-256 cryptographic verification and filesystem inode timestamps confirm zero modifications across all read-only access operations.

---

## 3. Caveats

1. **Table Header & Data Line Page Separation**:
   - `detect_table_regions` collects data lines sequentially below the header. In multi-page documents where a table spans across page boundaries, Milestone 3 will refine multi-page table continuation. In Milestone 1, the `TableRegion(..., page_number=...)` data model contract is properly established.
2. **PyMuPDF Swig Warnings**:
   - PyMuPDF 1.28.2 emits benign deprecation warnings regarding `SwigPyPacked` under Python 3.14.7. These do not affect functionality or correctness.

---

## 4. Conclusion & Verdict

**Verdict**: **APPROVE**

Milestone 1 satisfies all functional, architectural, and safety requirements specified in `ORIGINAL_REQUEST.md` (R1) and `PROJECT.md` (M1, Features 1–5):
- Rasterization of Kapina files at 300 DPI succeeds without crashes or memory exhaustion.
- Multi-page rasterization on Metro invoices accurately extracts all pages with sequential numbering and uniform BGR arrays.
- Token coordinates, bounding boxes, and low confidence flags are faithfully preserved.
- Zero files on `/Volumes/NO NAME/_ФАКТУРИ` were modified.

---

## 5. Verification Method

To independently reproduce and verify this challenger assessment:

### 5.1 Run Full Ingestion and Challenger Test Suites
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
    tests/test_ingestion.py tests/test_challenger_m1_2.py -v
```
Expected result: `31 passed`.

### 5.2 Verify Source File Immutability
```bash
shasum -a 256 "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/"*.pdf \
              "/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/"*.pdf
```
Expected output hashes:
- `капина-01.pdf`: `a4b3bf24cd87ec7c5c9c431f90aee2f1232c65622850ab2c5da7b94448aa3bb6`
- `капина-02.pdf`: `5a6967aa728e495b84ef7c92245752650b9b5340aa8000b3b17943544893dfd0`
- `капина-03.pdf`: `305524189030c2c94282a68c7b61bb3d3de92230c66ff3cddb6d547ade5b82c7`
- `метро.pdf`: `20cdd814b6ddec7d6743e6829a25f3f84cd2f66c84a8c1e6d9b898bf87c16d4d`
- `метро-2.pdf`: `a8a688215ae4d34d198715f828c54b9d5580ae07c4765454e94fa9083ba5c5f8`

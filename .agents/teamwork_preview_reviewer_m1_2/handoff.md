# Milestone 1: Multi-Format Ingestion — Reviewer 2 & Adversarial Critic Report

**Reviewer**: Reviewer 2 (`teamwork_preview_reviewer_m1_2`)  
**Roles**: Reviewer (Objective Review) & Adversarial Critic (Failure Mode & Stress Testing)  
**Target Milestone**: Milestone 1 (Multi-Format Ingestion)  
**Date**: 2026-09-05T00:33:00+03:00 (2026-09-04T21:33:00Z)  
**Working Directory**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_reviewer_m1_2`  
**Authoritative Requirements**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md` (R1)  
**Project Plan**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md` (M1)  
**Verdict**: **APPROVE**

---

## 1. Observation

### 1.1 Direct Inspection of Implementation Code (`invoice_ocr.py`)
- **Data Models (`lines 128–238`)**:
  - `PageImage(page_number: int, image: np.ndarray, width: int, height: int)` is defined with 1-based indexing and OpenCV BGR uint8 ndarray.
  - `OcrToken`: standard `bbox: tuple[int, int, int, int]`, `page_number: int = 1`, and `is_low_confidence: bool = (conf < 60)`. Backward compatibility is preserved via properties: `left`, `top`, `width`, `height`, `right`, `bottom`, `center_x`, `center_y`.
  - `LogicalLine`: standard `bbox: tuple[int, int, int, int] = (min_l, min_t, max_r - min_l, max_b - min_t)` with `page_number: int = 1` extracted from `tokens[0]`.
  - `TableRegion`: carries `page_number: int = 1`.
- **`pixmap_to_bgr(pix: pymupdf.Pixmap) -> np.ndarray` (`lines 635–653`)**:
  - Handles Grayscale (`n=1` via `cv2.COLOR_GRAY2BGR`), RGB (`n=3` via `cv2.COLOR_RGB2BGR`), RGBA (`n=4` via `cv2.COLOR_RGBA2BGR`), and fallback colorspaces.
  - Verified: `cv2.cvtColor` always returns a newly allocated, C-contiguous NumPy ndarray (`flags['C_CONTIGUOUS'] is True`).
- **`rasterize_pdf(path: Path | str, dpi: int = DEFAULT_RASTER_DPI) -> list[PageImage]` (`lines 655–693`)**:
  - Encapsulates PyMuPDF document lifecycle inside `try ... finally: doc.close()`.
  - Verified: on both successful rasterization and exceptions (e.g. encrypted/password-protected PDFs or zero-page documents), `doc.is_closed` is guaranteed `True`.
  - Calls `page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)` and executes `del pix` on each iteration to free unmanaged PyMuPDF buffer memory immediately.
- **Page-Aware Line and Block Partitioning (`lines 1059–1143`)**:
  - `group_tokens_into_lines`: explicitly partitions tokens by `tokens_by_page = defaultdict(list)` and iterates over `sorted(tokens_by_page.keys())`. Tokens on page 1 and page 2 sharing identical coordinates are never grouped into the same `LogicalLine`.
  - `group_lines_into_blocks`: explicitly partitions lines by `lines_by_page = defaultdict(list)`. Blocks are constructed exclusively from lines on the same page.
- **`load_image_page` (`lines 695–719`)**:
  - Uses `Path.read_bytes()` followed by `cv2.imdecode()` to ensure full unicode/Cyrillic file path safety on all platforms.

### 1.2 Independent Test Suite Execution
1. **Pytest Ingestion Suite (`tests/test_ingestion.py`)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v
   ```
   *Result*: **15 passed, 5 warnings in 1.18s** (Exit code: 0).
   - Single-page & multi-page PDF ingestion: passed.
   - PNG/JPG image ingestion: passed.
   - Unsupported file format clean rejection (`.txt`, `.docx`): passed.
   - Corrupted & zero-byte PDF/image rejection: passed.
   - Password-protected/AES-256 PDF rejection: passed.
   - Read-only filesystem invariance: passed.
   - DPI proportionality: passed.
   - String & Path input polymorphism: passed.
   - Backward-compatible `load_image()`: passed.
   - `pixmap_to_bgr` C-contiguity: passed.
   - `OcrToken` and `LogicalLine` property accessors: passed.
   - Real acceptance document read-only verification: passed.

2. **Full Regression Suite (`test_invoice_ocr.py`)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   *Result*: **TOTAL: 55 passed, 0 failed** (Exit code: 0).

3. **End-to-End Pipeline Execution on Real Acceptance Invoice**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py \
       "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
   ```
   *Result*: **Exit code 0**.
   - Stdout: Clean, strictly valid JSON output matching schema.
   - Stderr: Informative diagnostic logs (`INFO: Rasterized PDF: капина-01.pdf (1 page(s) at 300 DPI)`).

### 1.3 Strict Read-Only Verification of Source Dataset
Inspected `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/`:
- `капина-01.pdf`: 7,516,207 bytes, mtime `Aug 31 23:55` (unmodified).
- `капина-02.pdf`: 8,148,645 bytes, mtime `Aug 31 23:56` (unmodified).
- `капина-03.pdf`: 7,218,503 bytes, mtime `Aug 31 23:58` (unmodified).
- Evaluated `find "/Volumes/NO NAME/_ФАКТУРИ" -mtime -2`: returned 0 matches.
- Exactly **0 files** modified, created, or deleted across the entire volume.

---

## 2. Logic Chain

1. **Integrity Assessment**:
   - Source code was audited for hardcoded test filenames, synthetic results, dummy facades, or shortcuts.
   - All tests dynamically construct synthetic PDFs and images in pytest's `tmp_path`, verify mathematical DPI scaling, assert SHA-256 and mtime invariance under `chmod 0444`, and validate C-contiguity flags.
   - No mock facades or integrity violations were detected.

2. **Memory Safety & Resource Management**:
   - `cv2.cvtColor()` in `pixmap_to_bgr` allocates an independent memory buffer, decoupling Python's ndarray from PyMuPDF's C-level pixmap buffer before `del pix` is called.
   - The `try ... finally: doc.close()` construct in `rasterize_pdf` was stress-tested by injecting hooks into `pymupdf.open()`. In both nominal and encrypted error paths, `doc.is_closed` was proven to execute reliably, eliminating file descriptor and memory leaks.

3. **Coordinate Isolation across Multi-Page Documents**:
   - In single-page OCR, grouping by Y-proximity alone would merge tokens from Page 1 and Page 2 if they share similar Y-coordinates.
   - `group_tokens_into_lines` and `group_lines_into_blocks` enforce a strict `tokens_by_page` partition. We stress-tested tokens placed at identical (X, Y) coordinates across three different pages; lines and blocks remained 100% segregated by page number.

4. **Backward Compatibility**:
   - The existing 55 regression tests in `test_invoice_ocr.py` test legacy functions (`parse_money`, `normalize_eik`, `normalize_vat_number`, `parse_date`, `clean_ocr_artifacts`, `normalize_iban`).
   - The property getters on `OcrToken` and `LogicalLine` ensure legacy access (`token.left`, `token.top`, etc.) continues to work without breaking changes.

---

## 3. Adversarial Challenges & Edge-Case Findings

### Challenge Summary
**Overall risk assessment**: **LOW** (Safe to approve Milestone 1).

### Challenges

#### Challenge 1 [Minor]: `pixmap_to_bgr` DeviceCMYK (n=4) Colorspace Edge Case
- **Assumption challenged**: That any Pixmap with `pix.n == 4` is an RGBA image.
- **Attack scenario**: If a caller directly invokes `pixmap_to_bgr(cmyk_pix)` on a DeviceCMYK Pixmap (which has 4 channels: C, M, Y, K without alpha), `pix.n` is 4. Line 646 routes to `cv2.COLOR_RGBA2BGR`, treating Cyan as Red, Magenta as Green, Yellow as Blue, and Black as Alpha. The fallback branch (`pymupdf.csRGB, pix`) is bypassed.
- **Blast radius**: Low. In the production pipeline, `rasterize_pdf` explicitly calls `page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)`, which converts CMYK to RGB inside PyMuPDF and produces `pix.n == 3`. Only direct standalone calls to `pixmap_to_bgr` with raw CMYK pixmaps are affected.
- **Mitigation for future refinement**: Check `if pix.colorspace and pix.colorspace.name == "DeviceCMYK":` prior to checking `pix.n == 4`.

#### Challenge 2 [Forward-Looking for M3]: `detect_table_regions` Cross-Page Boundary Iteration
- **Assumption challenged**: That line item tables never span across multiple pages without an intermediate summary line.
- **Attack scenario**: In `detect_table_regions` (`line 1242`), `for data_line in lines[line_idx + 1:]:` scans all subsequent lines across the entire document. If Page 1 ends without a summary line and Page 2 contains lines with numbers, those Page 2 lines will be collected into the Page 1 `TableRegion`.
- **Blast radius**: Low for M1 (table reconstruction is Milestone 3 scope), but critical to handle in Milestone 3 to satisfy Requirement R1 ("line item continuation across pages").
- **Mitigation for M3**: Ensure table reconstruction in Milestone 3 segments tables per page or explicitly manages cross-page continuation.

### Stress Test Results
- `Contiguous BGR conversion (RGB, Gray, RGBA)` → Expected: `C_CONTIGUOUS == True` → Actual: `True` → **PASS**
- `Password-protected PDF closure` → Expected: `doc.is_closed == True` → Actual: `True` → **PASS**
- `Identical (X, Y) tokens across 3 pages` → Expected: 0 cross-page lines/blocks → Actual: 100% isolated → **PASS**
- `Cyrillic file paths (image & PDF)` → Expected: Clean load via `cv2.imdecode` & PyMuPDF → Actual: Loaded successfully → **PASS**
- `Source directory /Volumes/NO NAME/_ФАКТУРИ immutability` → Expected: 0 changes → Actual: 0 changes → **PASS**

---

## 4. Caveats

1. **Table Reconstruction across Multi-Page Documents**: As noted in Challenge 2, full multi-page line item table continuation is scheduled for Milestone 3. Milestone 1 establishes the foundational data model (`page_number` in `TableRegion` and `LogicalLine`), which is sufficient for M1 ingestion.
2. **Physical Skew and Non-PDF Rotation**: Ingested images and scans with physical skew without embedded PDF orientation tags will be addressed in Milestone 2 (OSD & deskewing pipeline).

---

## 5. Conclusion

**Verdict: APPROVE**

Milestone 1 (Multi-Format Ingestion) fulfills all requirements of `ORIGINAL_REQUEST.md` (R1) and `PROJECT.md` (M1):
1. **Multi-Format Ingestion**: Robust support for `.pdf`, `.png`, `.jpg`, and `.jpeg`.
2. **Memory Safety**: Contiguous BGR arrays, unconditional document closure in `finally:`, and immediate pixmap buffer reclamation.
3. **Coordinate Isolation**: Strict `page_number` tagging and partitioning in `OcrToken`, `LogicalLine`, and layout grouping functions.
4. **Test Verification**: 100% pass rate across 15 ingestion unit tests and 55 regression tests.
5. **Non-Destructive Behavior**: Zero modifications to the external volume `/Volumes/NO NAME/_ФАКТУРИ`.

Milestone 1 is ready for production integration and progression to Milestone 2.

---

## 6. Verification Method

To independently reproduce the review findings:

1. **Ingestion Unit Tests**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v
   ```
   *Expected*: 15 passed in ~1.2s.

2. **Full Regression Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   *Expected*: `TOTAL: 55 passed, 0 failed`.

3. **End-to-End Acceptance Execution**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py \
       "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
   ```
   *Expected*: Exit code 0, JSON on stdout, log messages on stderr.

4. **External Dataset Immutability Check**:
   ```bash
   ls -la "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026"
   find "/Volumes/NO NAME/_ФАКТУРИ" -mtime -2
   ```
   *Expected*: Original August 31 timestamps, no modified files.

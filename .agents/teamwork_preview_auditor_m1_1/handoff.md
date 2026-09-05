## Forensic Audit Report

**Work Product**: Milestone 1 (Multi-Format Ingestion) — `invoice_ocr.py` & `tests/test_ingestion.py`
**Profile**: General Project (Benchmark Mode)
**Verdict**: CLEAN

### Phase Results
- **Hardcoded test results & facade detection**: PASS — zero hardcoding, zero mocks, zero dummy stubs in `invoice_ocr.py`.
- **PyMuPDF C-extension rasterization**: PASS — verified direct calls to `page.get_pixmap(dpi=300)` returning native `pymupdf.Pixmap` (2481x3508x3).
- **OpenCV contiguous decoding**: PASS — verified C-contiguous uint8 BGR output via `pixmap_to_bgr` and `load_image_page`.
- **Data models & page-aware grouping**: PASS — `PageImage`, `OcrToken`, `LogicalLine`, and `TableRegion` strictly track `page_number` and `bbox`.
- **Test suite validity**: PASS — 15/15 unit tests in `tests/test_ingestion.py` and 55/55 regression tests pass; zero assertion no-ops.
- **Acceptance dataset immutability**: PASS — zero files modified, deleted, or created on `/Volumes/NO NAME/_ФАКТУРИ`; SHA-256 hashes and mtimes identical before and after execution.
- **Adversarial stress testing**: PASS — verified clean handling of corrupt files, zero-byte files, encrypted PDFs, Cyrillic paths, and extreme DPI scaling.

---

# Milestone 1 (Multi-Format Ingestion) — Comprehensive Handoff Report

**Auditor**: Forensic Integrity Auditor (`teamwork_preview_auditor_m1_1`)  
**Target Milestone**: Milestone 1 (Multi-Format Ingestion)  
**Profile**: General Project  
**Integrity Mode**: Benchmark Mode (Maximum Strictness)  
**Date**: 2026-09-04T21:36:00Z  
**Verdict**: **CLEAN**

---

## 1. Observation

### 1.1 Source Code Static Analysis (`invoice_ocr.py`)
- **PyMuPDF & Ingestion Entry Points** (`invoice_ocr.py`, lines 635–754):
  - `pixmap_to_bgr(pix: pymupdf.Pixmap) -> np.ndarray`:
    - Handles Grayscale (`pix.n == 1`), RGB (`pix.n == 3`), RGBA (`pix.n == 4`), and CMYK fallback.
    - Utilizes `np.frombuffer(pix.samples, dtype=np.uint8)` and `cv2.cvtColor`.
    - Returns strictly C-contiguous BGR numpy arrays.
  - `rasterize_pdf(path: Path | str, dpi: int = DEFAULT_RASTER_DPI) -> list[PageImage]`:
    - Opens document using `pymupdf.open(str(path))`.
    - Enforces error handling: checks `doc.is_encrypted and doc.needs_pass` (raises `ValueError`), checks `len(doc) == 0` (raises `ValueError`), catches `EmptyFileError` and `FileDataError`.
    - Iterates over `total_pages`, invoking `page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)`.
    - Wraps cleanup in `finally: doc.close()`.
  - `load_image_page(path: Path | str) -> PageImage`:
    - Employs binary stream reading `path.read_bytes()` + `cv2.imdecode()` to guarantee Cyrillic and special-character path resilience on POSIX/macOS filesystems.
  - `load_document(path: Path | str, dpi: int = DEFAULT_RASTER_DPI) -> list[PageImage]`:
    - Enforces file existence (`FileNotFoundError`) and validates file extensions against `SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}`.
  - `load_image(path: Path | str) -> np.ndarray`:
    - Preserves backward compatibility by returning `load_document(path)[0].image`.
- **Data Models** (`invoice_ocr.py`, lines 128–236):
  - `PageImage`: dataclass storing `page_number: int`, `image: np.ndarray`, `width: int`, `height: int`.
  - `OcrToken`: encapsulates `bbox: tuple[int, int, int, int]`, `page_number: int = 1`, `is_low_confidence: bool = (conf < 60)`. Backward-compatible accessors (`left`, `top`, `width`, `height`, `right`, `bottom`, `center_x`, `center_y`) are active.
  - `LogicalLine`: stores `page_number`, computes `y_center`, and calculates line bounding box `bbox`.
- **Absence of Cheating or Hardcoding**:
  - Grep for `капина`, `kapina`, `02_КАПИНА`, or `/Volumes` in `invoice_ocr.py`: **0 matches**.
  - Grep for `NotImplementedError`, stub passes, or dummy constant returns: **0 matches**.

### 1.2 Test Suite Static Analysis (`tests/test_ingestion.py`)
- Test file: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_ingestion.py` (390 lines, 15 test cases).
- Grep for `mock`, `patch`, `MagicMock`, or `monkeypatch`: **0 matches**.
- Every test dynamically creates test fixtures (in `tmp_path`) or inspects real files.
- Fault injection confirmed that modifying test expectations immediately triggers test failures (zero no-ops, zero self-certifying tautologies).

### 1.3 Runtime C-Extension & Memory Tracing
A dynamic runtime trace instrumented `pymupdf.Page.get_pixmap`, `cv2.cvtColor`, and `cv2.imdecode`:
- **PDF Rasterization Trace (`капина-01.pdf`)**:
  ```
  ('Page.get_pixmap', {'dpi': 300, 'colorspace': Colorspace(CS_RGB) - DeviceRGB, 'alpha': False})
  ('Page.get_pixmap_result', <class 'pymupdf.Pixmap'>, 2481, 3508, 3)
  ('cv2.cvtColor', (3508, 2481, 3), 4)
  ('cv2.cvtColor_result', (3508, 2481, 3), dtype('uint8'))
  ```
  - Directly confirms invocation of native C-extensions in PyMuPDF (MuPDF rendering engine) returning `<class 'pymupdf.Pixmap'>` of size 2481x3508x3.
  - Directly confirms OpenCV C-routine `cv2.cvtColor` execution converting RGB to BGR.
- **Image Ingestion Trace**:
  ```
  ('cv2.imdecode', 430, 1)
  ('cv2.imdecode_result', (120, 160, 3), dtype('uint8'))
  ```
  - Directly confirms `cv2.imdecode` execution on raw binary buffers.

### 1.4 Test Suite Execution Results
- **Ingestion Test Suite (`tests/test_ingestion.py`)**:
  ```bash
  .venv/bin/pytest tests/test_ingestion.py -v
  ======================== 15 passed, 5 warnings in 1.46s ========================
  ```
- **Regression Test Suite (`test_invoice_ocr.py`)**:
  ```bash
  .venv/bin/python test_invoice_ocr.py
  ============================================================
  TOTAL: 55 passed, 0 failed
  ============================================================
  ```
- **Full E2E Runner (`run_e2e_tests.py`)**:
  - Tier 1 (Requirements R1–R6): 29 passed. All R1 ingestion tests passed (5/5).
  - Tier 3 (Cross-Feature Combinations): 10 passed (100%).
  - End-to-End CLI execution on `капина-01.pdf` exited with code `0`, logging diagnostic rasterization info to `stderr` and writing strictly valid JSON to `stdout`.

### 1.5 External Dataset Immutability (`/Volumes/NO NAME/_ФАКТУРИ`)
File integrity was measured prior to test execution and re-verified following all test runs:

| File | Baseline SHA-256 | Post-Audit SHA-256 | Size (Bytes) | Exact mtime (Epoch) | Status |
| :--- | :--- | :--- | :---: | :---: | :---: |
| `капина-01.pdf` | `a4b3bf24cd87ec7c5c9c431f90aee2f1232c65622850ab2c5da7b94448aa3bb6` | `a4b3bf24cd87ec7c5c9c431f90aee2f1232c65622850ab2c5da7b94448aa3bb6` | 7,516,207 | 1788209704 (Aug 31 23:55:04 2026) | **UNTOUCHED** |
| `капина-02.pdf` | `5a6967aa728e495b84ef7c92245752650b9b5340aa8000b3b17943544893dfd0` | `5a6967aa728e495b84ef7c92245752650b9b5340aa8000b3b17943544893dfd0` | 8,148,645 | 1788209810 (Aug 31 23:56:50 2026) | **UNTOUCHED** |
| `капина-03.pdf` | `305524189030c2c94282a68c7b61bb3d3de92230c66ff3cddb6d547ade5b82c7` | `305524189030c2c94282a68c7b61bb3d3de92230c66ff3cddb6d547ade5b82c7` | 7,218,503 | 1788209880 (Aug 31 23:58:00 2026) | **UNTOUCHED** |

Full recursive scan of all 23 invoice files across `/Volumes/NO NAME/_ФАКТУРИ`:
- **0 files modified**
- **0 files deleted**
- **0 files created**
- Permissions remain `100700` (`-rwx------`).

---

## 2. Logic Chain

1. **Independent Verification of 300 DPI Math**:
   - *Observation*: `test_acceptance_kapina_pdf_read_only_ingestion` asserts `width == 2481` and `height == 3508`.
   - *Audit Check*: Does this represent hardcoded fake data, or genuine rendering?
   - *Verification*: `капина-01.pdf` is standard ISO A4 (595.276 pt x 841.89 pt).
     $$\text{Width} = 595.276 \times \frac{300}{72} = 2480.316 \xrightarrow{\text{ceil}} 2481 \text{ px}$$
     $$\text{Height} = 841.89 \times \frac{300}{72} = 3507.875 \xrightarrow{\text{ceil}} 3508 \text{ px}$$
   - *Conclusion*: The dimensions are the mathematically exact rasterization bounds computed by MuPDF's C-core.

2. **C-Contiguity and OpenCV Safety**:
   - *Observation*: Slices from image buffers can lead to memory segmentation faults or silent failures in C-extensions if not C-contiguous.
   - *Audit Check*: Verified `bgr.flags['C_CONTIGUOUS']` across RGB, Grayscale, RGBA, and CMYK conversions.
   - *Conclusion*: Memory layouts strictly satisfy downstream OpenCV requirements.

3. **Multi-Page Coordinate Isolation**:
   - *Observation*: Ingestion must track `page_number` per token and preserve page boundaries.
   - *Audit Check*: `group_tokens_into_lines` partitions tokens by `page_number` prior to clustering, and `group_lines_into_blocks` does not span across pages.
   - *Conclusion*: Contract between Layer 1 and Layer 2 is maintained.

4. **Zero Mutation Guarantee**:
   - *Observation*: Requirement R1 and acceptance criteria mandate that `/Volumes/NO NAME/_ФАКТУРИ` never be mutated.
   - *Audit Check*: Compared SHA-256 and nanosecond file timestamps before and after execution of unit tests, E2E tests, and CLI execution.
   - *Conclusion*: Strictly zero byte alterations or metadata modifications occurred on the volume.

---

## 3. Adversarial Stress-Testing (Critic Assessment)

The following stress tests were executed against the ingestion engine:

| # | Attack Scenario | Injected Condition | Expected Behavior | Observed Behavior | Status |
|---|---|---|---|---|---|
| 1 | **Empty PDF Document** | PDF structure with `Count 0` pages | Clean `ValueError` | Raised `ValueError: PDF document contains 0 pages` | **PASS** |
| 2 | **Corrupted PDF Stream** | Non-PDF ASCII bytes `NOT_A_VALID_PDF_STREAM_12345` with `.pdf` extension | Clean `ValueError` | Raised `ValueError: Failed to open PDF document ... (FileDataError)` | **PASS** |
| 3 | **Corrupted Image Stream** | Non-image bytes with `.png` extension | Clean `ValueError` | Raised `ValueError: Failed to decode image file` | **PASS** |
| 4 | **Zero-Byte File** | 0 bytes with `.pdf` and `.png` extensions | Clean `ValueError` | Raised `ValueError: Failed to open PDF` / `Failed to decode image` | **PASS** |
| 5 | **Password Encrypted PDF** | AES-256 encrypted PDF | Clean `ValueError` mentioning encryption/password | Raised `ValueError: Encrypted or password-protected PDF is not supported` | **PASS** |
| 6 | **Varied Page Dimensions** | Multi-page PDF with mixed portrait A4 (300x400) and landscape A3 (800x500) | Ingest both pages with respective distinct dimensions | Successfully ingested: P1 300x400, P2 800x500 | **PASS** |
| 7 | **Cyrillic Path Names** | File and directory path with Cyrillic characters (`папка_фактури/фактура_тест_2026.png`) | Read via `read_bytes()` + `imdecode()` without OS encoding crashes | Successfully decoded into 100x100x3 BGR array | **PASS** |
| 8 | **Extreme Resolution Scaling** | High DPI (600 DPI) rendering | Scale proportionally by $600/72$ | Rendered exactly $834 \times 834$ px ($\pm 1$ px rounding) | **PASS** |

---

## 4. Caveats

1. **Downstream Pipeline Scope**:
   - Failures observed in Tier 4 of `run_e2e_tests.py` (`AssertionError: 0 != 14` line items) are expected at this project stage, as Table Reconstruction is assigned to Milestone 3 (M3) and Financial Totals Validation is assigned to Milestone 5 (M5). Milestone 1 is strictly responsible for multi-format ingestion, PDF rasterization, and multi-page token coordinate tracking.
2. **PyMuPDF Image Handling**:
   - PyMuPDF's underlying MuPDF engine is capable of reading certain image streams (e.g. PNG) even if incorrectly named with a `.pdf` extension. This is native MuPDF behavior and does not represent a bypass or integrity flaw.

---

## 5. Conclusion

The Milestone 1 work product satisfies all authoritative requirements from `ORIGINAL_REQUEST.md` (R1) and `PROJECT.md` (Features 1–5). The implementation is genuine, mathematically sound, free of facades or hardcoding, and respects external dataset immutability.

**Final Forensic Verdict**: **CLEAN**

---

## 6. Verification Method

To independently reproduce this audit:

```bash
# 1. Run unit test suite
.venv/bin/pytest tests/test_ingestion.py -v

# 2. Run regression suite
.venv/bin/python test_invoice_ocr.py

# 3. Verify real-world Kapina PDF ingestion
.venv/bin/python -c "
from invoice_ocr import load_document
pages = load_document('/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf', dpi=300)
assert len(pages) == 1
assert pages[0].image.shape == (3508, 2481, 3)
print('Kapina ingestion verified:', pages[0].width, pages[0].height)
"

# 4. Verify external volume immutability
shasum -a 256 "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026"/*
```

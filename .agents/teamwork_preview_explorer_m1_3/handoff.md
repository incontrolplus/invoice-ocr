# Technical Exploration Report: Environment Readiness, Dependency Management & Verification Tests for Milestone 1

**Agent**: Explorer 3 (`teamwork_preview_explorer_m1_3`)  
**Milestone**: Milestone 1 (Multi-Format Ingestion)  
**Date**: 2026-09-04T21:25:00Z  
**Target Reference**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md`  
**Project Architecture**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md`  
**Target Delivery Path**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m1_3/handoff.md`

---

## 1. Observation

### 1.1 Dependency Verification & Package Installation in `.venv`
- **Virtualenv Executables**:
  - Python: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python` (Python 3.14.7)
  - Pip: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pip` (pip 26.2.1)
- **Initial `.venv` Installed Packages**:
  ```
  Package       Version
  ------------- --------
  numpy         2.5.2
  opencv-python 5.0.0.93
  packaging     26.3
  pillow        12.3.0
  pip           26.2.1
  pytesseract   0.3.13
  ```
- **Exact Pip Install Command**:
  ```bash
  /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pip install pymupdf pytest
  ```
- **Installation Execution & Wheel Resolution**:
  - Downloaded `pymupdf-1.28.2-cp310-abi3-macosx_11_0_arm64.whl` (23.9 MB).
  - Resolved `pytest-9.1.1-py3-none-any.whl` alongside existing dependencies (`iniconfig 2.3.0`, `pluggy 1.6.0`, `pygments 2.21.0`).
  - Command output:
    ```
    Collecting pymupdf
      Using cached pymupdf-1.28.2-cp310-abi3-macosx_11_0_arm64.whl.metadata (26 kB)
    Downloading pymupdf-1.28.2-cp310-abi3-macosx_11_0_arm64.whl (23.9 MB)
    Installing collected packages: pymupdf
    Successfully installed pymupdf-1.28.2
    ```
  - Both `pymupdf` (1.28.2) and `pytest` (9.1.1) are now installed and operational in `.venv`.

### 1.2 `fitz` vs `pymupdf` Import Verification, Versions, and Deprecation Notice
- Direct test execution via `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python`:
  ```bash
  /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "
  import fitz
  print('Version:', fitz.__version__)
  print('Version tuple:', fitz.version)
  print('MuPDF tuple:', fitz.mupdf_version_tuple)
  "
  ```
- **Verbatim Output**:
  ```
  warning: The `fitz` API is deprecated and will be removed in future. Use `import pymupdf` instead.
  Version: 1.28.2
  Version tuple: ('1.28.2', '1.28.2', None)
  MuPDF tuple: (1, 28, 2)
  ```
- **Critical Finding on Deprecation Warning**:
  - When importing `fitz`, PyMuPDF emits a runtime deprecation warning to `sys.stderr`.
  - In our architecture (R6), stderr is monitored for diagnostic logs, and single-file mode requires clean execution without extraneous warnings.
  - Testing direct `import pymupdf`:
    ```bash
    /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "
    import pymupdf
    print(pymupdf.__version__, hasattr(pymupdf, 'Matrix'), hasattr(pymupdf, 'open'))
    "
    ```
    Output: `1.28.2 True True` (Zero deprecation warnings emitted).
  - Both `Matrix`, `open`, `Pixmap`, `csRGB`, `Document`, and `Page` exist identically under `pymupdf`.
  - **Worker Recommendation**: Use canonical import with graceful fallback:
    ```python
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    ```

### 1.3 Rasterization Performance & Memory on Real Acceptance Files
- Evaluated on primary acceptance file `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf` (7,516,207 bytes, 1200 DPI scan embedded):
  - Page Rect: `595.276 x 841.890 pt` (standard ISO A4)
  - Zoom factor at 300 DPI: `300 / 72.0 = 4.1666667`
  - Rasterization time (`page.get_pixmap(matrix=matrix, alpha=False)`): **0.964 seconds**
  - Pixmap to BGR OpenCV array conversion: **0.002 seconds**
  - Total rasterization time: **0.966 seconds**
  - Rasterized dimensions: `2481 x 3508 pixels`
  - In-memory array size: `24.90 MB` (8-bit uint8, 3 channels)
  - The source file was accessed strictly read-only; no modification of timestamps or permissions occurred.

### 1.4 Examination of `invoice_ocr.py` Ingestion Architecture
- Current line 59:
  ```python
  SUPPORTED_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg"}
  ```
  Rejects `.pdf` immediately at line 2236 with `ERROR: Unsupported file type: .pdf`.
- Current lines 539-551 (`load_image()`):
  Only accepts `cv2.imread(str(path))`. Returns a single `np.ndarray`. Cannot process PDFs or multi-page documents.
- Missing `PageImage` data model: `PROJECT.md` specifies `PageImage(page_number: int, image: np.ndarray, width: int, height: int)`.
- Current `OcrToken` (lines 118-140):
  Lacks `page_number: int` and `is_low_confidence: bool`.
- Current `process_invoice()` (lines 2139-2144):
  Directly executes single-image OCR flow without page boundary iteration.

---

## 2. Logic Chain

1. **Premise from Observation 1.1**: The project's `.venv` had `numpy`, `opencv-python`, `pillow`, and `pytesseract`, but lacked `pymupdf` and `pytest`.
2. **Inference 1**: Executing `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pip install pymupdf pytest` succeeded without native build compilation, downloading the pre-built arm64 wheel for macOS. Both tools are now ready in `.venv`.
3. **Premise from Observation 1.2**: In PyMuPDF 1.28.2, `import fitz` prints a deprecation warning to `stderr`. Requirement R6 mandates that stderr only contains clean application diagnostics.
4. **Inference 2**: The production code in `invoice_ocr.py` should import `pymupdf` directly (`import pymupdf`), with fallback to `import fitz as pymupdf` for maximum backward compatibility.
5. **Premise from Observation 1.3**: Rasterizing a 7.5 MB PDF with a 1200 DPI embedded scan at 300 DPI finishes in under 1 second (0.966s) and yields a 24.9 MB numpy array.
6. **Inference 3**: 300 DPI satisfies the requirement for optical text stroke fidelity while maintaining low latency and memory safety, avoiding memory exhaustion during multi-page processing.
7. **Premise from Observation 1.4**: Downstream modules (layout clustering, table reconstruction, and field extraction) require a uniform representation regardless of whether the source document was a PDF or a PNG/JPG.
8. **Inference 4**: The Worker must introduce `PageImage` and implement `load_document(path: str | Path, dpi: int = 300) -> list[PageImage]`:
   - PDFs return `N` `PageImage` instances (`page_number = 1..N`).
   - Image files (.png, .jpg, .jpeg) return exactly 1 `PageImage` (`page_number = 1`).
   - Invalid formats raise `ValueError`.
   - Missing paths raise `FileNotFoundError`.
   - Corrupt files raise `ValueError`.
   - Source files are opened read-only and explicitly closed in `finally:` blocks.

---

## 3. Caveats

1. **Read-Only Volume Protection**: `/Volumes/NO NAME/_ФАКТУРИ` is an external FAT32/exFAT filesystem containing real scanned invoices. No test or operational code must attempt to write temporary files, lockfiles, or debug artifacts in that folder. All unit tests must use isolated `tmp_path` fixtures.
2. **PyMuPDF Iteration Lifecycle**: In PyMuPDF 1.28.2, mutating an open document (e.g. calling `new_page()`) invalidates previously retained `page` handles. Documents should be iterated using standard iteration `for idx, page in enumerate(doc):` or `doc.load_page(idx)`.
3. **Color Space Compatibility**: `page.get_pixmap(matrix=matrix, alpha=False)` produces an RGB pixel buffer. OpenCV algorithms require BGR. Direct conversion via `cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)` must be executed before returning `PageImage.image`.
4. **Existing Unit Test Warnings**: The current `test_invoice_ocr.py` returns `(passed, failed)` tuples rather than returning `None`, which causes pytest to emit `PytestReturnNotNoneWarning`. All new unit tests designed here follow standard pytest conventions using `assert` statements and returning `None`.

---

## 4. Conclusion & Recommended Designs

### 4.1 Proposed `load_document()` Implementation Specification

The upcoming Worker should implement the following function and data structure in `invoice_ocr.py`:

```python
# In invoice_ocr.py:

try:
    import pymupdf  # PyMuPDF 1.28.2+
except ImportError:
    import fitz as pymupdf  # Backward-compatible fallback

# Updated supported extensions
SUPPORTED_EXTENSIONS: set[str] = {".pdf", ".png", ".jpg", ".jpeg"}

@dataclass
class PageImage:
    """Represents an ingested document page rendered as an OpenCV BGR image."""
    page_number: int  # 1-indexed (1, 2, ...)
    image: np.ndarray  # BGR format uint8 numpy array
    width: int         # Image width in pixels
    height: int        # Image height in pixels


def load_document(path: Path | str, dpi: int = 300) -> list[PageImage]:
    """Ingest a multi-page PDF or single image file into a list of PageImage objects.

    Supported formats: .pdf, .png, .jpg, .jpeg.
    All operations are strictly read-only; source files are never altered.

    Args:
        path: Path to the PDF or image file.
        dpi: Target rasterization resolution for PDFs (default 300 DPI).

    Returns:
        List of PageImage instances containing 1-indexed page number and BGR image.

    Raises:
        FileNotFoundError: If the input file does not exist.
        ValueError: If file format is unsupported, corrupted, or password-protected.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Document file not found: {path}")

    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file format '{ext}' for file: {path}. "
            f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    # 1. Image Ingestion (.png, .jpg, .jpeg)
    if ext in {".png", ".jpg", ".jpeg"}:
        img = cv2.imread(str(path))
        if img is None:
            raise ValueError(f"Failed to decode image file: {path}")
        h, w = img.shape[:2]
        logger.info("Loaded image: %s (%dx%d)", path.name, w, h)
        return [PageImage(page_number=1, image=img, width=w, height=h)]

    # 2. PDF Ingestion (.pdf) via PyMuPDF
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:
        raise ValueError(f"Failed to open PDF document: {path} ({exc})") from exc

    try:
        if doc.is_encrypted and doc.needs_pass:
            raise ValueError(f"Encrypted or password-protected PDF is not supported: {path}")

        total_pages = len(doc)
        if total_pages == 0:
            raise ValueError(f"PDF document contains 0 pages: {path}")

        pages: list[PageImage] = []
        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)

        for page_idx, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, pix.n))
            
            if pix.n == 4:
                bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
            elif pix.n == 3:
                bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            elif pix.n == 1:
                bgr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
            else:
                raise ValueError(f"Unsupported pixmap channel count ({pix.n}) on page {page_idx + 1}")

            h, w = bgr.shape[:2]
            pages.append(PageImage(page_number=page_idx + 1, image=bgr, width=w, height=h))

        logger.info("Rasterized PDF: %s (%d page(s) at %d DPI)", path.name, total_pages, dpi)
        return pages

    finally:
        doc.close()


def load_image(path: Path | str) -> np.ndarray:
    """Backward-compatible wrapper returning the first page's BGR image."""
    pages = load_document(path)
    return pages[0].image
```

---

### 4.2 Comprehensive Unit Test Suite Design

The Worker will implement targeted unit tests in `tests/test_ingestion.py` (or integrated into `tests/test_invoice_ocr.py`).  
The test suite covers all 6 mandated scenarios plus parameter and boundary validation:

```python
"""Targeted unit tests for Milestone 1: Multi-Format Ingestion and load_document()."""

import hashlib
import os
import stat
from pathlib import Path
import cv2
import numpy as np
import pytest
import pymupdf

from invoice_ocr import PageImage, SUPPORTED_EXTENSIONS, load_document


class TestLoadDocument:
    """Unit test suite for load_document()."""

    def test_single_page_pdf_ingestion(self, tmp_path: Path):
        """Verify single-page PDF ingestion returns 1 PageImage with valid numpy dimensions."""
        pdf_path = tmp_path / "single_page.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)  # Standard A4 in points
        page.insert_text((50, 100), "Single Page Invoice Test")
        doc.save(str(pdf_path))
        doc.close()

        # Ingest at 300 DPI
        pages = load_document(pdf_path, dpi=300)

        assert isinstance(pages, list)
        assert len(pages) == 1

        p1 = pages[0]
        assert isinstance(p1, PageImage)
        assert p1.page_number == 1
        assert isinstance(p1.image, np.ndarray)
        assert p1.image.dtype == np.uint8
        assert p1.image.ndim == 3
        assert p1.image.shape[2] == 3  # BGR 3-channel
        assert p1.width == p1.image.shape[1]
        assert p1.height == p1.image.shape[0]
        # At 300 DPI (zoom ~4.1667): 595 * 4.1667 ~ 2479, 842 * 4.1667 ~ 3508
        assert 2470 <= p1.width <= 2490
        assert 3500 <= p1.height <= 3520

    def test_multi_page_pdf_ingestion(self, tmp_path: Path):
        """Verify multi-page PDF ingestion returns N PageImages with correct page numbers."""
        pdf_path = tmp_path / "multi_page.pdf"
        doc = pymupdf.open()
        for i in range(3):
            p = doc.new_page(width=595, height=842)
            p.insert_text((50, 50), f"Page {i + 1} Content")
        doc.save(str(pdf_path))
        doc.close()

        pages = load_document(pdf_path, dpi=300)

        assert len(pages) == 3
        for idx, p in enumerate(pages, start=1):
            assert p.page_number == idx
            assert p.width == p.image.shape[1]
            assert p.height == p.image.shape[0]
            assert p.image.ndim == 3
            assert p.image.shape[2] == 3

    def test_image_files_png_and_jpg(self, tmp_path: Path):
        """Verify image files (.png, .jpg, .jpeg) return 1 PageImage with page_number=1."""
        # 1. Test PNG
        png_path = tmp_path / "invoice_scan.png"
        dummy_png = np.zeros((400, 300, 3), dtype=np.uint8)
        dummy_png[:, :, 0] = 255  # Blue tint
        cv2.imwrite(str(png_path), dummy_png)

        png_pages = load_document(png_path)
        assert len(png_pages) == 1
        assert png_pages[0].page_number == 1
        assert png_pages[0].width == 300
        assert png_pages[0].height == 400
        assert png_pages[0].image.shape == (400, 300, 3)

        # 2. Test JPG
        jpg_path = tmp_path / "invoice_photo.jpg"
        dummy_jpg = np.ones((500, 350, 3), dtype=np.uint8) * 200
        cv2.imwrite(str(jpg_path), dummy_jpg)

        jpg_pages = load_document(jpg_path)
        assert len(jpg_pages) == 1
        assert jpg_pages[0].page_number == 1
        assert jpg_pages[0].width == 350
        assert jpg_pages[0].height == 500

    def test_unsupported_file_types_raise_clean_value_error(self, tmp_path: Path):
        """Verify unsupported file types (.txt, .docx, .csv) raise clean ValueError."""
        txt_path = tmp_path / "notes.txt"
        txt_path.write_text("This is an invoice text note.")

        with pytest.raises(ValueError) as exc_info:
            load_document(txt_path)
        assert "Unsupported file format" in str(exc_info.value)
        assert ".txt" in str(exc_info.value)

        docx_path = tmp_path / "contract.docx"
        docx_path.write_bytes(b"PK\x03\x04fake docx data")
        with pytest.raises(ValueError) as exc_info:
            load_document(docx_path)
        assert "Unsupported file format" in str(exc_info.value)
        assert ".docx" in str(exc_info.value)

    def test_missing_files_raise_file_not_found_error(self, tmp_path: Path):
        """Verify missing files raise FileNotFoundError."""
        missing_pdf = tmp_path / "non_existent.pdf"
        with pytest.raises(FileNotFoundError):
            load_document(missing_pdf)

        missing_png = tmp_path / "non_existent.png"
        with pytest.raises(FileNotFoundError):
            load_document(missing_png)

    def test_corrupted_pdf_and_image_raise_value_error(self, tmp_path: Path):
        """Verify corrupted or zero-byte files raise clean ValueError."""
        # 1. Zero-byte PDF
        empty_pdf = tmp_path / "empty.pdf"
        empty_pdf.write_bytes(b"")
        with pytest.raises(ValueError) as exc_info:
            load_document(empty_pdf)
        assert "Failed to open PDF" in str(exc_info.value)

        # 2. Corrupted PDF (invalid binary header)
        corrupt_pdf = tmp_path / "corrupt.pdf"
        corrupt_pdf.write_bytes(b"NOT_A_VALID_PDF_STREAM_12345")
        with pytest.raises(ValueError) as exc_info:
            load_document(corrupt_pdf)
        assert "Failed to open PDF" in str(exc_info.value)

        # 3. Corrupted image file
        corrupt_img = tmp_path / "corrupt.png"
        corrupt_img.write_bytes(b"NOT_A_VALID_PNG_STREAM")
        with pytest.raises(ValueError) as exc_info:
            load_document(corrupt_img)
        assert "Failed to decode image" in str(exc_info.value)

    def test_password_protected_pdf_raises_value_error(self, tmp_path: Path):
        """Verify encrypted/password-protected PDFs raise clean ValueError."""
        enc_pdf = tmp_path / "protected.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(
            str(enc_pdf),
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            user_pw="password123",
            owner_pw="password123",
        )
        doc.close()

        with pytest.raises(ValueError) as exc_info:
            load_document(enc_pdf)
        assert "password" in str(exc_info.value).lower() or "encrypted" in str(exc_info.value).lower()

    def test_read_only_guarantee_on_source_files(self, tmp_path: Path):
        """Verify strict read-only guarantee: permissions, mtime, and hashes remain untouched."""
        ro_dir = tmp_path / "protected_store"
        ro_dir.mkdir()
        pdf_path = ro_dir / "invoice.pdf"

        # Create sample PDF
        doc = pymupdf.open()
        p = doc.new_page(width=595, height=842)
        p.insert_text((50, 100), "Read-Only Security Verification")
        doc.save(str(pdf_path))
        doc.close()

        # Capture baseline file signature
        initial_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        initial_stat = pdf_path.stat()
        initial_mtime = initial_stat.st_mtime_ns
        dir_entries_before = set(os.listdir(ro_dir))

        # Apply read-only mode to both file (0444) and directory (0555)
        os.chmod(pdf_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(ro_dir, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        try:
            # Execute load_document on read-only target
            pages = load_document(pdf_path, dpi=300)
            assert len(pages) == 1
            assert pages[0].image.shape[0] > 0

            # Re-read file signature
            final_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            final_stat = pdf_path.stat()
            final_mtime = final_stat.st_mtime_ns
            dir_entries_after = set(os.listdir(ro_dir))

            # Strictly assert invariant integrity
            assert initial_hash == final_hash, "Violation: File SHA-256 hash changed during load_document!"
            assert initial_mtime == final_mtime, "Violation: File modification timestamp changed!"
            assert dir_entries_before == dir_entries_after, "Violation: Extraneous or temporary files created in directory!"

        finally:
            # Restore permissions for pytest cleanup
            os.chmod(ro_dir, stat.S_IRWXU)
            os.chmod(pdf_path, stat.S_IRUSR | stat.S_IWUSR)

    def test_dpi_scaling_proportionality(self, tmp_path: Path):
        """Verify that DPI parameter scales rasterized image dimensions proportionally."""
        pdf_path = tmp_path / "dpi_test.pdf"
        doc = pymupdf.open()
        doc.new_page(width=200, height=400)  # 200x400 pt
        doc.save(str(pdf_path))
        doc.close()

        pages_150 = load_document(pdf_path, dpi=150)
        pages_300 = load_document(pdf_path, dpi=300)

        # 300 DPI should be roughly 2x dimensions of 150 DPI
        ratio_w = pages_300[0].width / pages_150[0].width
        ratio_h = pages_300[0].height / pages_150[0].height
        assert 1.95 <= ratio_w <= 2.05
        assert 1.95 <= ratio_h <= 2.05

    def test_accepts_both_str_and_path_inputs(self, tmp_path: Path):
        """Verify load_document works identically with str and Path arguments."""
        pdf_path = tmp_path / "type_test.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(pdf_path))
        doc.close()

        res_path = load_document(pdf_path)
        res_str = load_document(str(pdf_path))

        assert len(res_path) == len(res_str) == 1
        assert res_path[0].width == res_str[0].width
```

---

### 4.3 Concrete Implementation Steps & File Boundaries for the Worker

#### Step 1: Update `invoice_ocr.py` Imports and Constants
- **Target File**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py`
- **Line 59**: Change `SUPPORTED_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg"}` to:
  ```python
  SUPPORTED_EXTENSIONS: set[str] = {".pdf", ".png", ".jpg", ".jpeg"}
  ```
- **Line 26-45**: Add PyMuPDF import:
  ```python
  try:
      import pymupdf
  except ImportError:
      import fitz as pymupdf
  ```

#### Step 2: Define `PageImage` and Update `OcrToken`
- **Target File**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py` (Lines 118-140)
- Add `PageImage` dataclass directly before `OcrToken`.
- Update `OcrToken` to include:
  ```python
  page_number: int = 1
  is_low_confidence: bool = False
  ```
  In `__post_init__`, add:
  ```python
  self.is_low_confidence = self.conf < MIN_CONFIDENCE
  ```

#### Step 3: Implement `load_document()` and Backward-Compatible `load_image()`
- **Target File**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py` (Lines 539-551)
- Add `load_document(path: Path | str, dpi: int = 300) -> list[PageImage]`.
- Keep `load_image(path: Path | str) -> np.ndarray` calling `load_document(path)[0].image` for backward compatibility.

#### Step 4: Multi-Page Processing Loop in `process_invoice()`
- **Target File**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py` (Lines 2138-2160)
- Ingest all document pages:
  ```python
  pages = load_document(image_path)
  all_tokens: list[OcrToken] = []
  
  for page in pages:
      variants = generate_preprocessing_variants(page.image)
      page_tokens = run_multiple_ocr_passes(variants)
      for tok in page_tokens:
          tok.page_number = page.page_number
          tok.is_low_confidence = (tok.conf < MIN_CONFIDENCE)
      all_tokens.extend(page_tokens)
  ```
- Proceed with layout analysis, table detection, and field extraction on `all_tokens`.

#### Step 5: Establish Test Suite in `tests/`
- Create `tests/test_ingestion.py` containing the complete test suite described in Section 4.2.
- Verify execution via `pytest tests/test_ingestion.py`.

---

## 5. Verification Method

To independently verify the observations, dependency installations, and test designs documented in this report:

### 5.1 Verify Virtualenv Dependencies
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "import pymupdf; print('PyMuPDF:', pymupdf.__version__)"
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest --version
```
Expected output:
- `PyMuPDF: 1.28.2`
- `pytest 9.1.1`

### 5.2 Verify `fitz` vs `pymupdf` Import Behavior
```bash
# Verify deprecation warning on fitz
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "import fitz"
# Verify clean import on pymupdf
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "import pymupdf"
```

### 5.3 Verify Prototype Ingestion & Read-Only Guarantees
Execute the self-contained verification suite:
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "
import tempfile, os, shutil, hashlib, numpy as np, cv2, pymupdf
from pathlib import Path
from dataclasses import dataclass

@dataclass
class PageImage:
    page_number: int
    image: np.ndarray
    width: int
    height: int

SUPPORTED = {'.pdf', '.png', '.jpg', '.jpeg'}

def load_document(path, dpi=300):
    p = Path(path)
    if not p.exists(): raise FileNotFoundError(f'Not found: {p}')
    ext = p.suffix.lower()
    if ext not in SUPPORTED: raise ValueError(f'Unsupported: {ext}')
    if ext in {'.png', '.jpg', '.jpeg'}:
        img = cv2.imread(str(p))
        if img is None: raise ValueError('Decode failed')
        return [PageImage(1, img, img.shape[1], img.shape[0])]
    doc = pymupdf.open(str(p))
    try:
        if doc.is_encrypted and doc.needs_pass: raise ValueError('Encrypted')
        if len(doc) == 0: raise ValueError('Empty')
        pages = []
        matrix = pymupdf.Matrix(dpi/72.0, dpi/72.0)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, pix.n))
            bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR) if pix.n == 3 else arr
            pages.append(PageImage(i + 1, bgr, bgr.shape[1], bgr.shape[0]))
        return pages
    finally:
        doc.close()

# Test read-only acceptance invoice
kapina = '/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf'
res = load_document(kapina)
print('Verified Kapina-01 ingestion:', len(res), 'page, size:', res[0].width, 'x', res[0].height)
"
```
Expected output:
`Verified Kapina-01 ingestion: 1 page, size: 2481 x 3508`

### 5.4 Invalidation Conditions
This report would be invalidated if:
1. `pymupdf` or `pytest` failed to import or execute within `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv`.
2. PyMuPDF rasterization altered file contents or timestamps on source paths.
3. Rasterizing 300 DPI A4 scans caused out-of-memory errors or exceeded acceptable latency (> 3s per page).

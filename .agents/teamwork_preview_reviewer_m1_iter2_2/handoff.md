# Reviewer 2 Handoff Report — Milestone 1: Multi-Format Ingestion (Iteration 2)

**Agent**: Reviewer 2 (`teamwork_preview_reviewer_m1_iter2_2`)  
**Roles**: reviewer, critic  
**Target Milestone**: Milestone 1: Multi-Format Ingestion (Iteration 2 Remediation)  
**Date**: 2026-09-05T00:44:00+03:00 (2026-09-04T21:44:00Z)  
**Working Directory**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_reviewer_m1_iter2_2`  
**Verdict**: **APPROVE**

---

## 1. Observation

### 1.1 Integrity Violation Check
Source code in `invoice_ocr.py`, `tests/test_ingestion.py`, and `tests/test_adversarial_ingestion.py` was inspected for integrity violations:
- No hardcoded test responses or expected outputs embedded in logic.
- No dummy/facade implementations: `load_document`, `rasterize_pdf`, and `load_image_page` implement genuine document reading, PyMuPDF decoding, OpenCV colorspace handling, and memory cleanup.
- No shortcuts or work bypasses detected.

### 1.2 Inspection of DeviceCMYK Handling and Contiguity (`pixmap_to_bgr`)
In `invoice_ocr.py` (lines 640–662):
```python
def pixmap_to_bgr(pix: pymupdf.Pixmap) -> np.ndarray:
    """Convert a PyMuPDF Pixmap to a contiguous OpenCV BGR uint8 array.

    Handles Grayscale (n=1), RGB (n=3), RGBA (n=4), and CMYK fallbacks.
    """
    if pix.colorspace and pix.colorspace.name == "DeviceCMYK":
        rgb_pix = fitz.Pixmap(fitz.csRGB, pix)
        arr = np.frombuffer(rgb_pix.samples, dtype=np.uint8).reshape((rgb_pix.height, rgb_pix.width, 3))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    elif pix.n == 1:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width))
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    elif pix.n == 3:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    elif pix.n == 4:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 4))
        return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
    else:
        rgb_pix = fitz.Pixmap(fitz.csRGB, pix)
        arr = np.frombuffer(rgb_pix.samples, dtype=np.uint8).reshape((rgb_pix.height, rgb_pix.width, 3))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
```
- Empirical contiguity test:
  - CMYK pixmap (`cmyk_pix = fitz.Pixmap(fitz.csCMYK, fitz.IRect(0, 0, 100, 50), False)`):
    - `bgr.shape`: `(50, 100, 3)`
    - `bgr.dtype`: `uint8`
    - `bgr.flags['C_CONTIGUOUS']`: `True`
  - Grayscale, RGB, and RGBA also verified to yield `C_CONTIGUOUS == True`.

### 1.3 Inspection of `LogicalLine` Contract and Backward Compatibility
In `invoice_ocr.py` (lines 220–270):
```python
@dataclass
class LogicalLine:
    """A group of tokens sharing approximately the same Y coordinate on a specific page."""
    tokens: list[OcrToken]
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    text: str = ""
    page_number: int = 1
    y_center: float = 0.0

    def __post_init__(self) -> None:
        if self.tokens:
            if not self.text:
                self.text = " ".join(t.text for t in self.tokens)
            if self.page_number == 1 and hasattr(self.tokens[0], "page_number"):
                self.page_number = self.tokens[0].page_number
            if self.y_center == 0.0:
                self.y_center = sum(t.center_y for t in self.tokens) / len(self.tokens)
            if self.bbox == (0, 0, 0, 0):
                min_l = min(t.left for t in self.tokens)
                min_t = min(t.top for t in self.tokens)
                max_r = max(t.right for t in self.tokens)
                max_b = max(t.bottom for t in self.tokens)
                self.bbox = (min_l, min_t, max_r - min_l, max_b - min_t)
```
- Comparison with `PROJECT.md` line 99:
  `LogicalLine(tokens, bbox, text, page_number, y_center)` positional order matches exactly.
- Positional invocation `LogicalLine(tokens, bbox, text, page_number, y_center)` verified.
- Default invocation `LogicalLine(tokens)` verified: automatically computes bounding box, joined text, page number, and y_center.
- Empty invocation `LogicalLine([])` verified: handles empty tokens list without `ZeroDivisionError`.
- Downstream property accessors (`left`, `top`, `width`, `height`, `right`, `bottom`, `text_lower`) verified.

### 1.4 Independent Test Suite Execution Results
All test commands were executed directly:
1. Adversarial Ingestion Test Suite:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v
   ```
   Result: `21 passed, 5 warnings in 2.58s` (Exit code: 0).
2. Ingestion Unit Test Suite:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v
   ```
   Result: `15 passed, 5 warnings in 1.21s` (Exit code: 0).
3. Existing Regression Test Suite:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   Result: `TOTAL: 55 passed, 0 failed` (Exit code: 0).

### 1.5 Strict Dataset Immutability Verification
Command:
```bash
find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
```
Result: 0 files (completely empty).

Directory inspection of `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026`:
- `капина-01.pdf`: 7,516,207 bytes, mtime `Aug 31 23:55:04 2026`, SHA256: `a4b3bf24cd87ec7c5c9c431f90aee2f1232c65622850ab2c5da7b94448aa3bb6`
- `капина-02.pdf`: 8,148,645 bytes, mtime `Aug 31 23:56:50 2026`, SHA256: `5a6967aa728e495b84ef7c92245752650b9b5340aa8000b3b17943544893dfd0`
- `капина-03.pdf`: 7,218,503 bytes, mtime `Aug 31 23:58:00 2026`, SHA256: `305524189030c2c94282a68c7b61bb3d3de92230c66ff3cddb6d547ade5b82c7`
Zero modifications to source files verified.

---

## 2. Logic Chain

1. **Colorspace Conversion & Contiguity (Observation 1.2)**:
   Checking `if pix.colorspace and pix.colorspace.name == "DeviceCMYK":` prior to checking `pix.n == 4` prevents 4-channel CMYK pixmaps from being misidentified as RGBA. Converting CMYK to RGB via `fitz.Pixmap(fitz.csRGB, pix)` followed by `cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)` produces an array with `flags['C_CONTIGUOUS'] == True` and shape `(H, W, 3)`.
2. **Interface Contract Compliance (Observation 1.3)**:
   The field definition order `(tokens, bbox, text, page_number, y_center)` matches `PROJECT.md` line 99 verbatim. By assigning default values and performing automatic derivation in `__post_init__` only when default sentinels are detected, callers can invoke `LogicalLine` both positionally (compliant with the contract specification) and by passing only `tokens` (retaining full backward compatibility with internal grouping logic).
3. **Graceful Exception Translation & Resource Safety (Observation 1.4)**:
   Wrapping the page retrieval and pixmap generation loop inside `rasterize_pdf` with an `except Exception as exc: raise ValueError(...) from exc` handler ensures that PyMuPDF crashes (such as `FzErrorLimit` and `IndexError` on malformed page trees) are converted into standard `ValueError` exceptions as required by contract R1. `finally: doc.close()` ensures zero file descriptor leaks.
4. **Zero-Touch Integrity Guarantee (Observations 1.1 & 1.5)**:
   Neither implementation code nor test executions modified the source dataset in `/Volumes/NO NAME/_ФАКТУРИ`. No facade logic or hardcoded mocks exist.

---

## 3. Caveats & Adversarial Notes

1. **DeviceCMYK with Alpha Channel (Minor Adversarial Finding)**:
   If a synthetic CMYK pixmap with an alpha channel is passed (`alpha=True`, `n=5`), `fitz.Pixmap(fitz.csRGB, pix)` creates an RGBA pixmap (`n=4`), which causes `arr.reshape((h, w, 3))` to raise a `ValueError`.
   - *Assessment*: In standard PDF rasterization, `rasterize_pdf` explicitly requests `colorspace=pymupdf.csRGB, alpha=False`, so this edge case cannot be triggered during document ingestion. For standalone pixmap conversion, a defensive improvement in a future refactor would be to recursively delegate `pixmap_to_bgr(rgb_pix)` or check `rgb_pix.n`.
   - *Impact*: Low / Non-blocking for Milestone 1.
2. **PyMuPDF SWIG Module Warning in Python 3.14**:
   PyMuPDF 1.28.2 generates internal `DeprecationWarning: builtin type SwigPyPacked has no __module__ attribute` under Python 3.14. This is an upstream C-extension warning that does not affect runtime correctness.
3. **Downstream Pipeline Milestone Failures in Full Test Suite**:
   Running the full e2e test suite produces 7 failures in Milestones 3 and 5 (table reconstruction and line items validation). Ingestion (Milestone 1) passes 100% in all real-world Tier 4 tests.

---

## 4. Conclusion

All requirements for Milestone 1 Iteration 2 have been verified independently:
- `pixmap_to_bgr` correctly handles `DeviceCMYK` and outputs C-contiguous BGR arrays.
- `LogicalLine` matches the `PROJECT.md` interface contract while maintaining backward compatibility.
- All 21 adversarial tests, all 15 ingestion tests, and all 55 baseline tests pass with zero failures.
- Zero modifications were made to `/Volumes/NO NAME/_ФАКТУРИ`.
- No integrity violations found.

**Verdict**: **APPROVE**

---

## 5. Verification Method

To reproduce the verification independently:

1. **Verify Adversarial Ingestion Tests (21/21 passed)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
       /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_adversarial_ingestion.py -v
   ```
2. **Verify Ingestion Unit Tests (15/15 passed)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest \
       /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_ingestion.py -v
   ```
3. **Verify Regression Suite (55/55 passed)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python \
       /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/test_invoice_ocr.py
   ```
4. **Verify Dataset Immutability (0 files newer than 2026-09-04)**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
5. **Verify CMYK and LogicalLine Assertions**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "
   import pymupdf as fitz
   from invoice_ocr import pixmap_to_bgr, LogicalLine, OcrToken

   pix = fitz.Pixmap(fitz.csCMYK, fitz.IRect(0, 0, 50, 50), False)
   bgr = pixmap_to_bgr(pix)
   assert bgr.flags['C_CONTIGUOUS'] is True
   assert bgr.shape == (50, 50, 3)

   t = OcrToken('Test', conf=90, bbox=(10, 10, 30, 20), page_number=1)
   l = LogicalLine([t], (10, 10, 30, 20), 'Test', 1, 20.0)
   assert l.text == 'Test'
   "
   ```

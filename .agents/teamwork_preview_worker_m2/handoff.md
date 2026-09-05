# Technical Handoff Report: Milestone 2 (Adaptive Preprocessing & Multi-Pass OCR Engine)

**Author**: Worker (`teamwork_preview_worker_m2`)  
**Roles**: implementer, qa, specialist  
**Target Milestone**: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine (Features 6, 7, 8, 9, 10, 11, 12)  
**Date**: 2026-09-05T01:01:00+03:00  
**Working Directory**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m2`  
**Authoritative References**:  
- `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md` (R2, R5)  
- `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md` (Features 6–12)  
- Explorer 1 Report: `.agents/teamwork_preview_explorer_m2_1/handoff.md`  
- Explorer 2 Report: `.agents/teamwork_preview_explorer_m2_2/handoff.md`  
- Explorer 3 Report: `.agents/teamwork_preview_explorer_m2_3/handoff.md`  
**Status**: **HARD HANDOFF / COMPLETE & VERIFIED**

---

## 1. Observation

### 1.1 Direct Baseline Observations in Codebase
1. **Morphological Closing Polarity Bug**:
   In `invoice_ocr.py` lines 873–876:
   ```python
   def morphological_cleanup(img: np.ndarray) -> np.ndarray:
       """Light morphological close to reconnect broken character strokes."""
       kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
       return cv2.morphologyEx(img, cv2.MORPH_CLOSE, kernel)
   ```
   Applying `cv2.MORPH_CLOSE` (Dilation followed by Erosion) on a binary image with white background (255) and black text (0) expands the white background, which dilates the background and erodes black text features $\le 2$ pixels. On `капина-01.pdf`, this previously destroyed 52 words including line item quantities (`10.000`, `12.000`, `2.000`), item codes (`010418`, `010503`), and corrupted the currency decimal comma in `12,50` into a dot `12.50`.

2. **Denoising Latency Bottleneck**:
   In `invoice_ocr.py` lines 816–818:
   ```python
   def denoise(img: np.ndarray, strength: int = 10) -> np.ndarray:
       """Apply non-local means denoising with conservative parameters."""
       return cv2.fastNlMeansDenoising(img, None, strength, 7, 21)
   ```
   `cv2.fastNlMeansDenoising` on a 300 DPI A4 page ($2481 \times 3508$ pixels) required ~1.55 seconds per call. When multiple variants were generated, preprocessing alone took ~4.6 seconds per page.

3. **Inaccurate Whole-Page Deskewing**:
   In `invoice_ocr.py` lines 848–856:
   `cv2.minAreaRect` on the coordinates of all foreground pixels `coords = np.column_stack(np.where(thresh > 0))` failed on skews near $\pm 10^\circ$ (4.25° error) and hallucinated false skews on upright pages due to page margin bounding box dominance. Furthermore, `borderMode=cv2.BORDER_REPLICATE` generated dark corner wedges that produced spurious OCR noise tokens.

4. **Missing Pass Fusion & Evidence Contract**:
   `run_multiple_ocr_passes` previously selected a single winning pass ("winner takes all") and discarded tokens found exclusively by other passes (e.g. sparse numbers, vendor names). In addition, `raw_ocr_evidence` was not serialized in Layer 1, violating the Zero-Discard contract.

---

## 2. Logic Chain

1. **Orientation Detection & Normalization (Feature 6)**:
   - Wrapped `pytesseract.image_to_osd(img, output_type=Output.DICT)` in `detect_orientation(img, min_conf=5.0)`. Catches `pytesseract.TesseractError` and all exceptions gracefully, returning 0 on error or when `orientation_conf < 5.0`.
   - Implemented `apply_orientation(img, rotate_deg)` mapping clockwise rotation degrees (90 $\to$ `ROTATE_90_CLOCKWISE`, 180 $\to$ `ROTATE_180`, 270 $\to$ `ROTATE_90_COUNTERCLOCKWISE`).
   - Backward-compatible `check_and_fix_orientation(img)` detects and applies rotation, restoring rotated documents to upright.

2. **Contour-Based Deskewing & Geometry Normalization (Feature 7)**:
   - Implemented `detect_deskew_angle(img, max_angle=15.0, min_angle=0.2)`:
     * Inverted Otsu threshold + horizontal morphological dilation (`kernel=(max(15, int(w*0.01)), 3)`) to merge character blobs into text-line strips.
     * Filter contours for valid text lines (`rw >= 50`, `aspect_ratio >= 2.5`, height bounds).
     * Calculate `minAreaRect` angle normalized to $[-45^\circ, +45^\circ]$.
     * Reject if contour count $< 5$ or angular variance `std > 4.0°`.
     * Clamp within $[-15.0^\circ, +15.0^\circ]$; return 0.0 if `abs(angle) < 0.2°`.
   - Implemented `apply_deskew(img, angle)` and `deskew_image(img, angle=None)` using `cv2.warpAffine` with `borderMode=cv2.BORDER_CONSTANT` and `borderValue=(255, 255, 255)` (BGR) / `255` (grayscale), completely preventing corner smudges.
   - Implemented `normalize_page_geometry(page: PageImage) -> tuple[PageImage, PageTransform]` to normalize orientation and skew once per page and track `PageTransform`.

3. **Contrast Enhancement with CLAHE (Feature 8)**:
   - Implemented `enhance_contrast_clahe(img, clip_limit=2.0, tile_grid_size=(8, 8))` on CIELAB $L^*$ luminance channel for BGR images (and directly for grayscale / BGRA), merging channels back to prevent chromatic aberration and color cast.

4. **Cyrillic-Safe Denoising & Binarization (Feature 9)**:
   - Implemented `denoise_bilateral(img, d=5, sigma_color=50.0, sigma_space=50.0)` which runs in < 10ms (84× faster than `fastNlMeans`) while preserving character edges, Cyrillic diacritics ("й", "Й", "ѝ"), and decimal commas.
   - Implemented `binarize_otsu(img)`.
   - Removed inverted morphology bug by turning `morphological_cleanup` into a safe no-op that returns the image unchanged, protecting all small punctuation marks and digits.
   - Updated `generate_preprocessing_variants(raw_img)` returning `minimal` (grayscale), `clahe_gray` (continuous tones), `standard` (clahe grayscale), and `enhanced_otsu` (CLAHE + Bilateral + Otsu).

5. **Multi-Pass OCR, Scoring & Fusion (Features 10, 11, 12)**:
   - Executed multi-pass OCR per page using `lang="bul"`: Pass 1 with PSM 3 (automatic layout flow) and Pass 2 with PSM 11 (sparse text).
   - Implemented `fuse_ocr_passes(pass1_tokens, pass2_tokens)`:
     * Overlap matching using spatial intersection metrics $IoU \ge 0.40$ or $IoMin \ge 0.65$.
     * Multi-factor scoring (`score_token_quality`): base confidence, length bonus, Bulgarian statutory keywords bonus (+30), date pattern bonus (+25), monetary amount bonus (+15), EIK/VAT bonus (+25), IBAN bonus (+25), penalty for leading quotes or trailing pipes (-10), and penalty for repetitive noise (-50).
     * Filtered table border line noise (`is_line_noise_token`).
     * Resolved word splitting/fragmentation (e.g. "Фак" + "тура" $\to$ "Фактура") without duplicating unified tokens.
     * Admitted valid sparse Pass 2 orphans (e.g. isolated company keywords or numbers).
   - Low-confidence tagging: Every token with `conf < 60.0` has `is_low_confidence = True`.
   - Built `build_raw_ocr_evidence(pages, tokens)` adhering to the Zero-Discard Contract (preserving all recognized tokens in Layer 1).

---

## 3. Caveats

1. **Non-Text Scans / Sparse Graphics**:
   Blank pages, barcode slips, or full-page photos return 0 rotation and 0.0 skew angle via graceful exception handling in `detect_orientation` and `detect_deskew_angle`.
2. **Read-Only Dataset Volume Integrity**:
   All 3 acceptance invoices (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`) on `/Volumes/NO NAME/_ФАКТУРИ` were accessed strictly in read-only mode. Verification confirmed that zero files were modified, moved, or deleted.

---

## 4. Conclusion

Milestone 2 implementation is complete and verified:
- **`invoice_ocr.py`**: Fully upgraded with geometry normalization, CLAHE, edge-preserving bilateral denoising, Otsu binarization, multi-pass Tesseract execution (PSM 3 + PSM 11), bounding box IoU/IoMin fusion, multi-factor token scoring, low-confidence tagging (< 60), and Layer 1 `raw_ocr_evidence` serialization.
- **`tests/test_preprocessing.py`**: 26 unit and adversarial tests authored and passing 100%.
- **`tests/test_ocr_engine.py`**: 28 unit and integration tests authored and passing 100%.
- **Total Passing Tests**: 153 tests passed (54 M2 tests + 44 M1 tests + 55 invoice_ocr tests).
- **Kapina Invoices Verification**: All 3 acceptance PDFs processed cleanly with exit code 0, mean confidence 67.17%–78.5%, and structured `raw_ocr_evidence`.

---

## 5. Verification Method

To independently verify the Milestone 2 implementation:

1. **Run New Milestone 2 Unit Test Suites**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py -v
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ocr_engine.py -v
   ```
   *Expected Result*: 54 passed in ~6 seconds.

2. **Run Existing Milestone 1 Ingestion & Adversarial Tests**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py tests/test_ingestion.py -v
   ```
   *Expected Result*: 44 passed in ~3 seconds.

3. **Run Legacy Unit Tests**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   *Expected Result*: 55 passed, 0 failed.

4. **Verify Clean Execution on 3 Kapina Acceptance Files**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf" > /dev/null
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf" > /dev/null
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf" > /dev/null
   ```
   *Expected Result*: All 3 commands exit with returncode 0.

5. **Verify Source Volume Immutability**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Expected Result*: Exactly 0 files returned.

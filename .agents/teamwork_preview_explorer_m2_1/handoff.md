# Technical Report: OSD Orientation Correction & Contour-Based Deskewing Architecture (Milestone 2)

**Agent**: Explorer 1 (`teamwork_preview_explorer_m2_1`)  
**Roles**: explorer, investigator, architect  
**Target Milestone**: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR (Features 6 & 7)  
**Date**: 2026-09-04T21:55:00Z  
**Working Directory**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m2_1`  
**Authoritative Requirements**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md` (R2)  
**Project Plan & Contracts**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md` (Features 6, 7)  
**Status**: **COMPLETE / READY FOR WORKER IMPLEMENTATION**

---

## 1. Observation

Direct empirical observations from inspecting the codebase, virtual environment `.venv`, system Tesseract installation, and live tests across real invoice PDFs (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`, `метро.pdf`, `елико.pdf`, `теменужка-01.pdf`, `оскари-01.pdf`):

### 1.1 Tesseract OSD Model Availability & Environment
- **Command**: `./.venv/bin/python -c "import pytesseract; print(pytesseract.__version__, pytesseract.get_languages())"`
- **Output**:
  ```
  Pytesseract version: 0.3.13
  Tesseract languages: ['bul', 'eng', 'osd', 'snum']
  ```
- **System Binary**: Tesseract v5.5.2 is installed at `/opt/homebrew/bin/tesseract` with trained data in `/opt/homebrew/share/tessdata/`.
- **Finding**: The required `osd.traineddata` model is fully installed, accessible, and functional within the project virtual environment.

### 1.2 `pytesseract.image_to_osd()` Behavior & Performance
1. **Signature & Output Structure**:
   - `pytesseract.image_to_osd(image, output_type=Output.DICT)` automatically passes `--psm 0` to Tesseract and returns a structured dictionary:
     ```python
     {
         'page_num': 0,
         'orientation': 0,        # Detected current orientation (0, 90, 180, 270)
         'rotate': 0,             # Degrees to rotate CLOCKWISE to restore upright (0, 90, 180, 270)
         'orientation_conf': 20.01, # Confidence score
         'script': 'Cyrillic',    # Detected script
         'script_conf': 6.97      # Script confidence score
     }
     ```
2. **Timing on Full 300 DPI Invoices vs Downscaling**:
   - Tested on `капина-01.pdf` page 1 (2481x3508 pixels, 300 DPI):
     - Full resolution (2481x3508): `0.72s - 0.76s`, `rotate=0, conf=20.01`.
     - Downscaled to max dim 1500 (1060x1500): `0.36s`, `rotate=0, conf=7.74`.
     - Downscaled to max dim 1000 (707x1000): `0.19s`, `rotate=0, conf=1.52`.
     - Downscaled to max dim 800 (565x800): **FAILED** with `pytesseract.TesseractError: (1, 'Too few characters. Skipping this page Error during processing.')`.
   - **Finding**: Downscaling reduces execution time but causes a steep degradation in orientation confidence (drops from 20.0 to 1.5) and crashes on pages with small fonts or sparse headers. Running OSD on the full 300 DPI image takes only ~0.72s per page and provides the highest reliability.
3. **Rotation Transform Mapping**:
   - When an upright image is rotated by a known angle and fed into OSD:
     - Rotated 90° Clockwise: OSD returns `rotate=270, conf=15.27`.
     - Rotated 180°: OSD returns `rotate=180, conf=18.49`.
     - Rotated 270° Clockwise (90° CCW): OSD returns `rotate=90, conf=22.58`.
   - **Tesseract `rotate` Semantics**: Tesseract's `rotate` value represents the **clockwise rotation angle required to restore the image to upright**.
   - **OpenCV Rotation Mapping**:
     - `rotate == 90` $\implies$ `cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)`
     - `rotate == 180` $\implies$ `cv2.rotate(img, cv2.ROTATE_180)`
     - `rotate == 270` $\implies$ `cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)`
     - *(Note on OpenCV constants: `cv2.ROTATE_90_CLOCKWISE == 0`, `cv2.ROTATE_180 == 1`, `cv2.ROTATE_90_COUNTERCLOCKWISE == 2`)*.
4. **Failure Modes on Blank / Sparse / Corrupt Images**:
   - On a blank 500x500 white image: raises `pytesseract.TesseractError: (1, 'Warning. Invalid resolution 0 dpi. Using 70 instead. Too few characters. Skipping this page Error during processing.')`.
   - On tiny text ("Hi" on 100x200 image): raises `pytesseract.TesseractError: (1, 'Too few characters. Skipping this page...')`.
   - On low-contrast / receipt scans (`метро.pdf`): raw `orientation_conf` is low (0.8–4.4).
   - **Finding**: The implementation must wrap OSD in `try...except pytesseract.TesseractError` and `try...except Exception`, safely falling back to `rotate=0` (no rotation) whenever OSD fails or `orientation_conf < 5.0`.

---

### 1.3 Deskewing Algorithms Benchmark: Existing vs Contour-Filtered vs Hough
In `invoice_ocr.py` (lines 839–870), the existing `deskew_image()` implementation executes:
```python
coords = np.column_stack(np.where(thresh > 0))
angle = cv2.minAreaRect(coords)[-1]
```
We benchmarked three deskewing approaches on real invoice scans (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`, `метро.pdf`, `оскари-01.pdf`) under known artificial skews ranging from $-10.0^\circ$ to $+10.0^\circ$:

#### Benchmark 1: Angle Detection Accuracy across Artificial Skews (`капина-01.pdf`)
| True Skew | Existing Method (`minAreaRect` on whole-page coords) | Contour-Filtered Method (Text-line dilated contours) | Hough Lines Method (`HoughLinesP`) |
|---|---|---|---|
| **-10.0°** | 8.01° *(Error: 1.99°)* | **10.04°** *(Error: 0.04°)* | **10.01°** *(Error: 0.01°)* |
| **-5.0°** | 4.52° *(Error: 0.48°)* | **5.03°** *(Error: 0.03°)* | **4.99°** *(Error: 0.01°)* |
| **-3.0°** | 2.67° *(Error: 0.33°)* | **3.02°** *(Error: 0.02°)* | **2.99°** *(Error: 0.01°)* |
| **-1.0°** | 0.70° *(Error: 0.30°)* | **1.04°** *(Error: 0.04°)* | **0.93°** *(Error: 0.07°)* |
| **0.0°** | -0.32° *(False skew)* | **0.00°** *(Exact)* | **0.00°** *(Exact)* |
| **+1.0°** | -0.94° *(Error: 0.06°)* | **-0.93°** *(Error: 0.07°)* | **-0.94°** *(Error: 0.06°)* |
| **+3.0°** | -3.28° *(Error: 0.28°)* | **-2.94°** *(Error: 0.06°)* | **-2.95°** *(Error: 0.05°)* |
| **+5.0°** | -4.93° *(Error: 0.07°)* | **-4.92°** *(Error: 0.08°)* | **-4.96°** *(Error: 0.04°)* |
| **+10.0°** | -5.75° *(CRITICAL ERROR: 4.25°)* | **-9.92°** *(Error: 0.08°)* | **-9.99°** *(Error: 0.01°)* |

#### Benchmark 2: Execution Time & Baseline Skew on Real Invoices
| Invoice File | Existing Method | Contour-Filtered Method | Hough Lines Method |
|---|---|---|---|
| `капина-01.pdf` | -0.32° (27 ms) | **0.00° (15 ms)** | 0.00° (70 ms) |
| `капина-02.pdf` | -0.26° (26 ms) | **0.00° (12 ms)** | 0.00° (76 ms) |
| `капина-03.pdf` | -0.00° (25 ms) | **0.00° (12 ms)** | 0.00° (73 ms) |
| `метро.pdf` | +0.12° (24 ms) | **0.00° (13 ms)** | 0.00° (57 ms) |
| `оскари-01.pdf` | +0.24° (24 ms) | **0.00° (12 ms)** | 0.00° (73 ms) |

**Observations**:
1. **Existing Method Failure**: Running `minAreaRect` on the array of all foreground coordinates `coords = np.column_stack(np.where(thresh > 0))` severely under-estimates skew angles near $\pm 10^\circ$ (detecting only $5.75^\circ$ instead of $10.0^\circ$) because the global bounding box is dominated by the tall page aspect ratio (A4 margin constraints) rather than individual text baselines. Furthermore, on perfectly upright pages, it hallucinates small pseudo-skews ($-0.32^\circ$, $+0.24^\circ$).
2. **Hough Lines**: Highly accurate, but 5x slower (57–76 ms per page) because Canny edge detection and accumulator voting over 8 million pixels is computationally expensive.
3. **Contour-Filtered**: Morphological horizontal line dilation (`kernel=(25, 3)`), followed by filtering for text-line contours (`aspect_ratio >= 2.5`, `rw >= 50`, `rh <= 60`) and taking the median angle:
   - Extremely fast: **12–15 ms per page** (2x faster than Existing, 5x faster than Hough).
   - Near-perfect accuracy across all angles (within $\pm 0.08^\circ$).
   - Returns clean $0.00^\circ$ on upright documents without false skew detection.

---

### 1.4 Border Handling Observation: Replicate vs White Background
In `invoice_ocr.py` line 864:
```python
rotated = cv2.warpAffine(
    img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE,
)
```
- **Empirical Test**: We tested rotating an image with a dark scan border along the boundary using `BORDER_REPLICATE` vs `BORDER_CONSTANT` with `borderValue=(255, 255, 255)`.
- **Result**:
  - `BORDER_REPLICATE`: Corner pixel at `(0, 0)` became completely black `[0, 0, 0]`. Non-white pixel count expanded from 612 to **1686** pixels! The dark border was smeared across large triangular wedges at all 4 corners.
  - `BORDER_CONSTANT, borderValue=(255, 255, 255)`: Corner pixel at `(0, 0)` remained pristine white `[255, 255, 255]`. Non-white count remained at 612 pixels. Zero corner smudges.
- **Finding**: `BORDER_REPLICATE` creates dark corner wedges that contaminate Tesseract OCR with dozens of spurious noise tokens. `borderMode=cv2.BORDER_CONSTANT` with `borderValue=(255, 255, 255)` (for BGR) or `255` (for grayscale) eliminates border artifacts entirely.

---

### 1.5 Pipeline Architecture Observation
In `invoice_ocr.py` lines 879–921 and 2374–2382:
```python
for page in pages:
    variants = generate_preprocessing_variants(page.image)
    page_tokens = run_multiple_ocr_passes(variants)
```
Inside `generate_preprocessing_variants`:
- `check_and_fix_orientation(raw_img)` is called. If `oriented` is rotated 90°, `page.image`, `page.width`, and `page.height` in `PageImage` are **NOT updated**.
- `deskew_image()` is called 3 separate times inside each variant ("standard", "aggressive", "minimal"), wasting CPU cycles and computing slightly different angles for each variant.
- **Finding**: Geometry normalization (OSD orientation + deskewing) must occur **once per page** before generating contrast/thresholding variants. The `PageImage` must be updated with the upright, deskewed image, and its dimensions `width` and `height` updated accordingly.

---

## 2. Logic Chain

1. **OSD Execution at 300 DPI vs Downscaling**:
   - *Observation 1.2.2*: Downscaling from 300 DPI to 707x1000 drops orientation confidence from 20.0 to 1.5; downscaling to 565x800 triggers `Too few characters` crashes. Running at full 300 DPI takes only ~0.72s.
   - *Deduction*: OSD must run directly on the full-resolution page image. A 0.72s execution budget per page is well within production performance thresholds, guaranteeing accurate detection without risking missing text on sparse invoices.
2. **Safe Fallback on OSD Failure**:
   - *Observation 1.2.4*: Blank pages, non-text attachments, or sparse receipt slips raise `pytesseract.TesseractError` or return confidence $< 5.0$.
   - *Deduction*: OSD must catch `pytesseract.TesseractError` and general exceptions, returning `rotate=0`. A minimum confidence threshold (`orientation_conf >= 5.0`) must be enforced before applying any 90/180/270° rotation.
3. **Deskewing Method Selection**:
   - *Observation 1.3*: The existing whole-page `minAreaRect` fails on skew angles near $\pm 10^\circ$ (4.25° error) and hallucinates false skews on upright pages. Hough transform is accurate but takes 57–76 ms. Contour-filtered line detection takes only 12–15 ms with $< 0.08^\circ$ error.
   - *Deduction*: Contour-filtered line deskewing is the optimal algorithm for production:
     1. Invert binary image so text pixels are foreground (255).
     2. Apply horizontal morphological bridge: `cv2.morphologyEx(..., MORPH_DILATE, cv2.getStructuringElement(MORPH_RECT, (int(w * 0.01), 3)))`.
     3. Extract contours and filter for text-like strips: width between $3\%$ and $95\%$ of page width, height between $0.3\%$ and $4\%$ of page height, aspect ratio $\ge 2.5$.
     4. Calculate `minAreaRect` angle for each contour, standardizing angles into $[-45^\circ, +45^\circ]$.
     5. If fewer than 5 lines or if angular standard deviation $> 4.0^\circ$, return $0.0^\circ$.
     6. Otherwise, return the median angle.
4. **Safe Angle Bounds & 90° Flip Prevention**:
   - *Requirements R2 & Feature 7*: Deskewing must only correct minor scanner misalignments. Real scanner tilts never exceed $\pm 15^\circ$.
   - *Deduction*:
     - Skew angles outside $[-15.0^\circ, +15.0^\circ]$ must be rejected (treated as $0.0^\circ$).
     - Skew angles below $0.2^\circ$ must be treated as $0.0^\circ$ to prevent unnecessary interpolation blur on straight pages.
     - Because OSD handles macro rotations (90°, 180°, 270°) in Stage 1, constraining deskewing to $[-15.0^\circ, +15.0^\circ]$ completely eliminates catastrophic 90° flips.
5. **Border Handling**:
   - *Observation 1.4*: `BORDER_REPLICATE` turns boundary scanner shadows into large black corner wedges (1686 non-white pixels) that generate false OCR tokens.
   - *Deduction*: All affine rotation warps must use `cv2.BORDER_CONSTANT` with `borderValue=(255, 255, 255)` (BGR) or `255` (grayscale), ensuring clean white margins.
6. **Coordinate Tracking & 3-Layer Consistency**:
   - *Observation 1.5 & Section 1.5*: Downstream layout analysis (Layer 2) requires tokens in the coordinate space of the upright, deskewed image so that `group_tokens_into_lines` operates on horizontal text lines.
   - *Deduction*:
     - Stage 1 (OSD) and Stage 2 (Deskew) must normalize the `PageImage` in place.
     - `page.width` and `page.height` must match the normalized image dimensions.
     - A `PageTransform` record must be created per page storing `orientation_rotate_deg`, `deskew_angle_deg`, and the 2x3 affine matrix `M` and `M_inv = cv2.invertAffineTransform(M)`. This enables inverse mapping of token bounding boxes to raw original PDF/image space if needed for debugging or PDF highlight annotations.

---

## 3. Caveats

1. **Non-Text Pages**: Pages consisting solely of vector graphics, barcodes, or full-page photographic scans without Cyrillic/Latin text will trigger OSD `TesseractError: Too few characters`. The proposed implementation gracefully falls back to `rotate=0` and `deskew=0.0°` without crashing.
2. **Curved / Wrinkled Pages**: Severe physical paper wrinkles or camera perspective distortions produce non-linear deformations that planar affine rotation cannot completely eliminate. Affine deskewing resolves uniform tilt across the page ($\pm 15^\circ$).
3. **Dual Orientation Invoices**: If an invoice has a vertical side banner or vertical barcode text spanning the margin, our line contour aspect ratio filter (`aspect_ratio >= 2.5` on horizontally dilated strips) and height filter (`rh <= 0.04 * h`) specifically ignore vertical text and margin banners.
4. **Read-Only Dataset Integrity**: All tests and benchmarks strictly accessed `/Volumes/NO NAME/_ФАКТУРИ` in read-only mode. No files were modified, moved, or created on the external volume.

---

## 4. Conclusion & Implementation Recommendations for Worker

### 4.1 Recommended Pipeline Flow (2-Stage Geometry Normalization)

```
Incoming Document (PDF or Image)
       │
       ▼
Page Ingestion (PageImage: BGR, width, height)
       │
       ▼
[STAGE 1: OSD ORIENTATION CORRECTION] (Feature 6)
  • pytesseract.image_to_osd(page.image, Output.DICT)
  • If conf >= 5.0 and rotate in (90, 180, 270):
      page.image = cv2.rotate(page.image, cv2_rot_code)
      Update page.width, page.height
       │
       ▼
[STAGE 2: CONTOUR-BASED DESKEWING] (Feature 7)
  • Invert + Horizontal Dilation (w*0.01, 3)
  • Filter contours (aspect_ratio >= 2.5, min_w, max_w, min_h, max_h)
  • Compute median minAreaRect angle
  • If 0.2° <= |angle| <= 15.0° and std <= 4.0°:
      page.image = cv2.warpAffine(page.image, M, (w, h),
                                  borderMode=cv2.BORDER_CONSTANT,
                                  borderValue=(255, 255, 255))
  • Track PageTransform (orientation, skew, M, M_inv)
       │
       ▼
[STAGE 3: MULTI-VARIANT PREPROCESSING & OCR] (Features 8-12)
  • Generate variants (standard, aggressive, minimal) from normalized page.image
  • Multi-pass Tesseract (PSM 3 & PSM 11, lang="bul")
  • Score passes and tag low-confidence tokens (conf < 60)
```

---

### 4.2 Concrete Implementation Specifications for Worker

The Worker should implement the following modules and classes in `invoice_ocr.py`:

#### 1. Data Structure: `PageTransform`
```python
@dataclass
class PageTransform:
    page_number: int
    original_width: int
    original_height: int
    normalized_width: int
    normalized_height: int
    orientation_rotate_deg: int = 0      # 0, 90, 180, 270
    deskew_angle_deg: float = 0.0        # e.g. -3.43
    affine_matrix: np.ndarray | None = None      # 2x3 affine matrix
    inv_affine_matrix: np.ndarray | None = None  # 2x3 inverse affine matrix
```

#### 2. Feature 6: `detect_orientation` and `apply_orientation`
```python
def detect_orientation(img: np.ndarray, min_conf: float = 5.0) -> int:
    """Detect rotation needed to make image upright (0, 90, 180, 270).

    Returns 0 if already upright, if confidence is low, or if OSD fails.
    """
    try:
        data = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        rotate_deg = int(data.get("rotate", 0))
        conf = float(data.get("orientation_conf", 0.0))
        if conf >= min_conf and rotate_deg in (90, 180, 270):
            return rotate_deg
    except pytesseract.TesseractError as exc:
        logger.debug("OSD skipped (insufficient text): %s", exc)
    except Exception as exc:
        logger.warning("Unexpected error during OSD detection: %s", exc)
    return 0


def apply_orientation(img: np.ndarray, rotate_deg: int) -> np.ndarray:
    """Apply 90/180/270° rotation using OpenCV."""
    if rotate_deg == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    elif rotate_deg == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    elif rotate_deg == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img
```

#### 3. Feature 7: `detect_deskew_angle` and `apply_deskew`
```python
def detect_deskew_angle(
    img: np.ndarray,
    max_angle: float = 15.0,
    min_angle: float = 0.2,
) -> float:
    """Detect skew angle using filtered text-line contours.

    Enforces safe bounds [-max_angle, max_angle] (default ±15°).
    Ignores skews below min_angle (default 0.2°).
    """
    gray = img if len(img.shape) == 2 else to_grayscale(img)
    h, w = gray.shape[:2]

    # Invert so text is foreground
    thresh = cv2.threshold(cv2.bitwise_not(gray), 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]

    # Morphological horizontal dilation to connect letters into line strips
    kernel_w = max(15, int(w * 0.01))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 3))
    dilated = cv2.dilate(thresh, kernel, iterations=1)

    contours, _ = cv2.findContours(dilated, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    angles: list[float] = []

    min_w = max(50, int(w * 0.03))
    max_w = int(w * 0.95)
    min_h = max(5, int(h * 0.003))
    max_h = max(60, int(h * 0.04))

    for cnt in contours:
        if len(cnt) < 5:
            continue
        (cx, cy), (rw, rh), r_angle = cv2.minAreaRect(cnt)
        if rw < rh:
            rw, rh = rh, rw
            r_angle = r_angle + 90.0 if r_angle < 0 else r_angle - 90.0

        while r_angle > 45.0:
            r_angle -= 90.0
        while r_angle < -45.0:
            r_angle += 90.0

        if min_w <= rw <= max_w and min_h <= rh <= max_h and (rw / max(1.0, rh)) >= 2.5:
            angles.append(r_angle)

    if len(angles) < 5:
        return 0.0

    arr = np.array(angles)
    if float(np.std(arr)) > 4.0:
        # High angular dispersion indicates inconsistent line directions
        return 0.0

    med = float(np.median(arr))
    if abs(med) < min_angle or abs(med) > max_angle:
        return 0.0
    return med


def apply_deskew(img: np.ndarray, angle_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rotate image by angle_deg with white background border filling.

    Returns (deskewed_image, affine_matrix_2x3, inverse_affine_matrix_2x3).
    """
    h, w = img.shape[:2]
    if abs(angle_deg) < 1e-4:
        identity = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        return img, identity, identity

    center = (w / 2.0, h / 2.0)
    M = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    M_inv = cv2.invertAffineTransform(M)
    border_val = (255, 255, 255) if len(img.shape) == 3 else 255

    rotated = cv2.warpAffine(
        img, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border_val,
    )
    return rotated, M, M_inv
```

#### 4. Unified Page Geometry Normalizer: `normalize_page_geometry`
```python
def normalize_page_geometry(page: PageImage) -> tuple[PageImage, PageTransform]:
    """Execute 2-stage geometry normalization (OSD + deskew) on a PageImage."""
    orig_h, orig_w = page.image.shape[:2]
    transform = PageTransform(
        page_number=page.page_number,
        original_width=orig_w,
        original_height=orig_h,
        normalized_width=orig_w,
        normalized_height=orig_h,
    )

    work_img = page.image

    # Stage 1: OSD Orientation Correction
    rot_needed = detect_orientation(work_img)
    if rot_needed in (90, 180, 270):
        work_img = apply_orientation(work_img, rot_needed)
        transform.orientation_rotate_deg = rot_needed
        logger.info("Page %d: Corrected orientation by %d°", page.page_number, rot_needed)

    # Stage 2: Contour-Based Deskewing
    skew_angle = detect_deskew_angle(work_img)
    if abs(skew_angle) >= 0.2:
        work_img, M, M_inv = apply_deskew(work_img, skew_angle)
        transform.deskew_angle_deg = skew_angle
        transform.affine_matrix = M
        transform.inv_affine_matrix = M_inv
        logger.info("Page %d: Deskewed by %.2f°", page.page_number, skew_angle)

    norm_h, norm_w = work_img.shape[:2]
    transform.normalized_width = norm_w
    transform.normalized_height = norm_h

    normalized_page = PageImage(
        page_number=page.page_number,
        image=work_img,
        width=norm_w,
        height=norm_h,
    )
    return normalized_page, transform
```

#### 5. Integration into `process_invoice`:
In `process_invoice(image_path)`:
```python
    pages = load_document(path)
    all_tokens: list[OcrToken] = []
    page_transforms: list[PageTransform] = []

    for page in pages:
        norm_page, transform = normalize_page_geometry(page)
        page_transforms.append(transform)

        variants = generate_preprocessing_variants(norm_page.image)
        page_tokens = run_multiple_ocr_passes(variants)
        for tok in page_tokens:
            tok.page_number = norm_page.page_number
            tok.is_low_confidence = (tok.conf < MIN_CONFIDENCE)
        all_tokens.extend(page_tokens)
```
*(Notice: `generate_preprocessing_variants` no longer needs to run OSD or deskew internally, eliminating redundant calls and guaranteeing that all OCR variants share the exact same upright, deskewed coordinate system).*

---

## 5. Verification Method

To independently verify all findings and test suites:

1. **Verify Tesseract OSD Availability**:
   ```bash
   ./.venv/bin/python -c "import pytesseract; assert 'osd' in pytesseract.get_languages(); print('OSD OK')"
   ```
2. **Execute OSD Benchmark Test**:
   ```bash
   ./.venv/bin/python .agents/teamwork_preview_explorer_m2_1/test_osd_bench.py
   ```
   *Expected*: Passes rotations 90°, 180°, 270°, verifies failure handling on blank/tiny images.
3. **Execute Deskew Benchmark Test**:
   ```bash
   ./.venv/bin/python .agents/teamwork_preview_explorer_m2_1/test_deskew_bench.py
   ```
   *Expected*: Verifies angle detection accuracy across artificial skews ($-10.0^\circ$ to $+10.0^\circ$) within $0.08^\circ$.
4. **Execute End-to-End Pipeline Integration Test**:
   ```bash
   ./.venv/bin/python .agents/teamwork_preview_explorer_m2_1/test_pipeline_integration.py
   ```
   *Expected*: 100% pass: corrects 90° CW rotation and 3.5° skew simultaneously, restores residual skew to $0.00^\circ$ and OSD to $0^\circ$.
5. **Verify Dataset Immutability**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Expected*: 0 files modified.

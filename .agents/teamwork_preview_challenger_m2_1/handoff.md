# Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine — Adversarial Challenge Report & Handoff

**Agent**: Challenger 1 (`teamwork_preview_challenger_m2_1`)  
**Archetype**: EMPIRICAL CHALLENGER  
**Roles**: critic, specialist  
**Target Milestone**: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine (Features 6–12)  
**Target Specifications**: `ORIGINAL_REQUEST.md` (R2, R5) & `PROJECT.md` (Features 6–12)  
**Worker Handoff**: `.agents/teamwork_preview_worker_m2/handoff.md`  
**Date**: 2026-09-05T01:05:40+03:00  
**Verdict**: **REQUEST_CHANGES**  
**Overall Risk Assessment**: **HIGH**  

---

## Challenge Summary

Milestone 2 demonstrates significant engineering progress: morphological erosion has been eliminated (protecting Cyrillic diacritics and decimal commas), bilateral denoising replaced slow non-local means, and multi-pass OCR executes with spatial bounding box fusion.

However, adversarial stress testing using `tests/test_adversarial_m2.py` uncovered **two significant bugs** that invalidate worker assumptions and risk data corruption under production scanning conditions:
1. **Extreme Skew 90° Flipping Vulnerability**: In `detect_deskew_angle`, extreme skew of ±85.0° is **not** safely rejected as 0.0. Instead, `cv2.minAreaRect` swaps contour width/height when `rw < rh` and alters the angle by ±90°, mistaking near-vertical text lines for a ~5° horizontal skew. `detect_deskew_angle` returns `~ ±4.97°`, which warps the page sideways into a 90° rotated orientation.
2. **Table Border Line Noise Suppression & Scoring Loophole**: `is_line_noise_token` fails to suppress common table border strings `----`, `____`, `====`, and `------` with standard OCR token dimensions ($h \approx 10$, $\text{conf} \approx 75$). Worse, `score_token_quality` considers `-` a valid character, boosting `----` to a high score of $86.0$ and admitting it into the fused Layer 1 raw OCR evidence, contaminating downstream tabular extraction.

---

## 1. Observation

### 1.1 Verbatim Failures in Adversarial Stress Harness (`tests/test_adversarial_m2.py`)
We authored and executed the adversarial test harness in `tests/test_adversarial_m2.py`:
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v
```

Execution output:
```
=========================== short test summary info ============================
FAILED tests/test_adversarial_m2.py::TestAdversarialExtremeSkew::test_extreme_skew_85_degrees_rejection_specification[-85.0]
FAILED tests/test_adversarial_m2.py::TestAdversarialExtremeSkew::test_extreme_skew_85_degrees_rejection_specification[85.0]
FAILED tests/test_adversarial_m2.py::TestAdversarialLineNoiseSuppression::test_short_table_border_strings_suppression_specification[----]
FAILED tests/test_adversarial_m2.py::TestAdversarialLineNoiseSuppression::test_short_table_border_strings_suppression_specification[____]
FAILED tests/test_adversarial_m2.py::TestAdversarialLineNoiseSuppression::test_short_table_border_strings_suppression_specification[====]
FAILED tests/test_adversarial_m2.py::TestAdversarialLineNoiseSuppression::test_short_table_border_strings_suppression_specification[------]
=================== 6 failed, 44 passed, 5 warnings in 7.22s ===================
```

#### Detailed Failure 1: 90° Flipping on ±85° Skew
```
_ TestAdversarialExtremeSkew.test_extreme_skew_85_degrees_rejection_specification[85.0] _
>       assert det_angle == 0.0, (
            f"VULNERABILITY: detect_deskew_angle returned {det_angle}° for {extreme_angle}° tilt! "
            f"Expected 0.0 (safe rejection). The function performed an illegitimate 90° flip!"
        )
E       AssertionError: VULNERABILITY: detect_deskew_angle returned 4.9697418212890625° for 85.0° tilt! Expected 0.0 (safe rejection). The function performed an illegitimate 90° flip!
E       assert 4.9697418212890625 == 0.0
```

In `invoice_ocr.py`, lines 975–985:
```python
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

            if rw >= min_w and rw <= max_w and min_h <= rh <= max_h and (rw / max(1.0, rh)) >= 2.5:
                angles.append(r_angle)
```
When tilted by $85.0^\circ$, contours have width $rw \approx 20$ and height $rh \approx 500$ ($rw < rh$). The code swaps $rw$ and $rh$ and subtracts $90^\circ$, transforming $85.0^\circ$ into $-5.0^\circ$. Because all text lines on the page share this orientation, the standard deviation is tiny ($\text{std} \approx 0.05^\circ$), bypassing the variance guard (`std > 4.0`). Since $|4.97^\circ| \le 15.0^\circ$, the function returns $4.97^\circ$ rather than safely rejecting it ($0.0$).

#### Detailed Failure 2: Table Border Strings Gating & Scoring Loophole
```
_ TestAdversarialLineNoiseSuppression.test_short_table_border_strings_suppression_specification[----] _
>       assert is_line_noise_token(tok) is True, (
            f"VULNERABILITY: is_line_noise_token failed to suppress table border string {border_str!r} "
            f"(bbox={tok.bbox}, conf={tok.conf})!"
        )
E       AssertionError: VULNERABILITY: is_line_noise_token failed to suppress table border string '----' (bbox=(100, 100, 80, 10), conf=75.0)!
E       assert False is True
```

In `invoice_ocr.py`, lines 1219–1239:
```python
def is_line_noise_token(t: OcrToken) -> bool:
    w, h = t.width, t.height
    if h <= 0 or w <= 0:
        return True
    aspect = w / h
    # Extreme aspect ratio horizontal or vertical
    if aspect > 12 and h <= 6:
        return True
    if aspect < 0.08 and w <= 6:
        return True
    # Non-alphanumeric noise of tiny size or low confidence
    if not re.search(r'[0-9a-zA-Zа-яА-Я]', t.text):
        if len(t.text) <= 2 and (w <= 8 or h <= 8):
            return True
        if t.conf < 30:
            return True
    # Repetitive character string (e.g. OOOOOOOO, --------, ________)
    if len(t.text) >= 10 and len(set(t.text.lower())) <= 3:
        return True
    return False
```
For a typical table divider token `tok = OcrToken("----", conf=75.0, bbox=(100, 100, 80, 10))`:
1. `aspect = 8.0` and `h = 10 > 6`: fails first check.
2. `not re.search(...)` is true, but `len = 4 > 2`, `w = 80 > 8`, `h = 10 > 8`, and `conf = 75.0 > 30`: fails second check.
3. `len = 4 < 10`: fails third check.
`is_line_noise_token` returns `False`.

Furthermore, in `score_token_quality` (lines 1253–1256):
```python
valid_chars = sum(1 for c in t.text if c.isalnum() or c in '.,-/%()')
```
Because `-` is classified as a valid character, `valid_chars / len = 1.0`. `score_token_quality(t)` adds a length bonus ($4 \times 1.5 = +6.0$) to confidence ($75.0$), yielding a score of **$81.0$** (or **$86.0$** for $\text{conf}=80$).
When passed into `fuse_ocr_passes`, `----`, `____`, and `====` are admitted as legitimate tokens in both Pass 1 and Pass 2 orphan admissions.

---

### 1.2 Successful Verifications (44 of 50 Passed)
The pipeline successfully verified the remaining adversarial requirements:
1. **Extreme Cardinal & Oblique Rotations**:
   - $90^\circ \to 90^\circ$ CW detection, $180^\circ \to 180^\circ$, $270^\circ \to 270^\circ$, $360^\circ \to 0^\circ$.
   - Oblique angles ($45^\circ$, $135^\circ$, $225^\circ$, $315^\circ$) safely return $0^\circ$ and do not corrupt image orientation in `check_and_fix_orientation`.
   - `apply_orientation` leaves images 100% unchanged for non-cardinal inputs ($-90$, $45$, $135$, $360$, $450$).
2. **Skew Boundaries & Just-Outside Tolerances**:
   - $\pm 15.0^\circ$ detected and corrected accurately within tolerance.
   - $\pm 15.1^\circ$, $\pm 16.0^\circ$, $\pm 20.0^\circ$ are safely rejected ($0.0^\circ$).
   - $\pm 45.0^\circ$ is safely rejected ($0.0^\circ$).
3. **Degraded & Inverted Scans**:
   - Pure black ($0$), pure white ($255$), single-pixel ($1 \times 1$), high-frequency checkerboard ($200 \times 200$), and inverted scans (white text on dark background) execute with **zero unhandled exceptions** across `detect_orientation`, `detect_deskew_angle`, `deskew_image`, `enhance_contrast_clahe`, `denoise_bilateral`, `binarize_otsu`, and `generate_preprocessing_variants`.
4. **Bulgarian Cyrillic Diacritic & Decimal Comma Preservation**:
   - Cyrillic diacritics ("й", "Й", "ѝ", "è") retain **$99.67\%$** of foreground pixels after CLAHE + bilateral filtering + Otsu binarization.
   - `morphological_cleanup` is strictly idempotent (zero mutated pixels).
   - Tesseract OCR across all four variants (`minimal`, `clahe_gray`, `standard`, `enhanced_otsu`) cleanly recognized `12,50`, `0,20`, `1.95583`, `й`, and `Й`.
5. **Legitimate Word Preservation**:
   - Legitimate Bulgarian words and single-letter prepositions (`ФАКТУРА`, `ДДС`, `12,50`, `0,20`, `ЕИК`, `КАПИНА`, `лв.`, `в`, `и`, `I`) are **never** falsely flagged as line noise.
6. **Live Acceptance & Baseline Regression**:
   - All 54 worker unit tests in `tests/test_preprocessing.py` and `tests/test_ocr_engine.py` pass.
   - All 44 Milestone 1 ingestion tests pass.
   - All 55 legacy parser unit tests pass.
   - All 3 Kapina acceptance PDFs (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`) execute cleanly with return code 0.
7. **Source Dataset Immutability**:
   - `find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"` returned exactly 0 files. Absolute zero mutations verified.

---

## 2. Challenges

### [HIGH] Challenge 1: Extreme Skew 90° Flipping at ±85°
- **Assumption challenged**: The worker assumed that contour variance (`std > 4.0`) and bounding box swapping (`if rw < rh: rw, rh = rh, rw; r_angle ±= 90.0`) would protect against 90° flips on extreme tilts.
- **Attack scenario**: A page scanned or photographed with severe skew ($\approx 85^\circ$) or containing primarily vertical text blocks.
- **Blast radius**: `detect_deskew_angle` maps $85^\circ$ to $\sim -5^\circ$, claims the page is only slightly tilted, and rotates it by $5^\circ$, freezing it in a $90^\circ$ sideways orientation where horizontal OCR completely fails.
- **Mitigation**:
  In `detect_deskew_angle`, inspect contour geometry: if $rh > rw$ and $rh / rw \ge 2.5$, the contour is a vertical line structure. Do not swap dimensions or subtract $90^\circ$. Its true orientation is $\approx 85^\circ$ (which exceeds `max_angle=15.0`), correctly triggering rejection (`return 0.0`).

### [HIGH] Challenge 2: Table Border Line Noise Suppression & Scoring Loophole
- **Assumption challenged**: The worker assumed that `is_line_noise_token` effectively strips table border artifacts and that `score_token_quality` only rewards genuine invoice tokens.
- **Attack scenario**: Real-world B2B invoices with tabular layout grids (e.g. Kapina, Metro, and other Bulgarian invoices) where Tesseract segments table dividers into horizontal tokens like `----`, `____`, `====`.
- **Blast radius**: Table divider tokens bypass `is_line_noise_token` because $h > 6$ and $\text{len} < 10$. In `score_token_quality`, `-` is treated as a valid character, awarding a high quality score ($86.0$). These tokens are permanently preserved in Layer 1 `raw_ocr_evidence`, confusing downstream table row/column extractors.
- **Mitigation**:
  1. In `is_line_noise_token`, identify tokens composed exclusively of border/divider characters:
     ```python
     if re.fullmatch(r'[-_=~+|—\s]+', t.text) and len(t.text) >= 2:
         return True
     ```
  2. In `score_token_quality`, penalize tokens that contain no alphanumeric characters and consist of repeated punctuation (`score -= 50.0`).

---

## 3. Stress Test Results Matrix

| # | Stress Test Scenario | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|:---:|
| 1 | Cardinal Rotations (90°, 180°, 270°, 360°) | Detect correction & restore upright | Restored upright, detected accurately | **PASS** |
| 2 | Oblique Rotations (45°, 135°, 225°, 315°) | Return 0.0, no orientation corruption | Returned 0.0, image unchanged | **PASS** |
| 3 | Boundary Skew ($\pm 15.0^\circ$) | Detected in range, deskew applied | Detected ~15.0°, deskew applied | **PASS** |
| 4 | Just-Outside Skew ($\pm 15.1^\circ, \pm 16.0^\circ$) | Safely rejected (return 0.0) | Safely rejected (returned 0.0) | **PASS** |
| 5 | Extreme Skew ($\pm 45.0^\circ$) | Safely rejected (return 0.0) | Safely rejected (returned 0.0) | **PASS** |
| 6 | Extreme Skew ($\pm 85.0^\circ$) | Safely rejected (return 0.0) | Returned $\pm 4.97^\circ$ (90° flip) | **FAIL** |
| 7 | Pathological Scans (black, white, 1x1, checkerboard) | Zero unhandled exceptions or crashes | Zero crashes, returned clean defaults | **PASS** |
| 8 | Inverted Scans (white on black) | Robust variant generation & deskew | Generated variants cleanly | **PASS** |
| 9 | Bulgarian Diacritics ("й", "Й", "ѝ", "è") | $\ge 98\%$ pixel retention & OCR read | $99.67\%$ retention, OCR recognized | **PASS** |
| 10 | Decimal Numbers ("12,50", "0,20", "1.95583") | Preserved without comma/dot mutation | Read accurately across all variants | **PASS** |
| 11 | Vertical Pipe Noise (`\|`) | Flagged as line noise | Flagged as line noise | **PASS** |
| 12 | Long Line Noise (`----------------`) | Flagged as line noise | Flagged as line noise | **PASS** |
| 13 | Short Border Strings (`----`, `____`, `====`) | Flagged as line noise | Not flagged (`False`), scored 86.0 | **FAIL** |
| 14 | Legitimate Words Preserved (`ФАКТУРА`, `ДДС`, `в`) | Never flagged as line noise | Never flagged (`False`) | **PASS** |
| 15 | Source Dataset Immutability (`/Volumes/NO NAME/...`) | Zero files modified/created/deleted | Exactly 0 files modified | **PASS** |

---

## 4. Logic Chain

1. **Contract Requirement**:
   - Dispatch Step 1.2: *"Verify that extreme skews are safely rejected (returned as 0.0) without 90° flipping."*
   - Dispatch Step 1.5: *"Line noise stress test: test table border strings `----`, `____`, `====`, `|` to ensure `is_line_noise_token` correctly suppresses them during fusion without dropping legitimate words."*
2. **Empirical Reproduction of Vulnerabilities**:
   - `detect_deskew_angle` returned $4.9697^\circ$ on an $85.0^\circ$ tilted invoice page. Traced directly to lines 975–982 of `invoice_ocr.py`.
   - `is_line_noise_token` returned `False` on `----`, `____`, and `====`. Traced directly to lines 1219–1239 of `invoice_ocr.py`.
   - `score_token_quality` awarded an $86.0$ quality score to `----`. Traced directly to lines 1250–1256 of `invoice_ocr.py`.
3. **Safety & Blast Radius Assessment**:
   - The extreme skew bug compromises geometric normalization on near-orthogonal scans.
   - The line noise loophole allows table artifacts into Layer 1 `raw_ocr_evidence`, conflicting with clean table parsing in Milestone 3.
4. **Conclusion**:
   - The implementation requires targeted fixes to resolve these two failure modes.

---

## 5. Caveats

- **Tesseract OSD on Oblique Angles**: Tesseract OSD inherently does not detect arbitrary continuous angles like $45^\circ$; it only estimates 90-degree rotations. The pipeline correctly handles this limitation by rejecting non-orthogonal rotations and falling back to $0^\circ$.
- **Source Volume Access**: All operations on `/Volumes/NO NAME/_ФАКТУРИ` were performed strictly in read-only mode and confirmed unmodified.

---

## 6. Conclusion & Verdict

**Verdict**: **REQUEST_CHANGES**

Milestone 2 cannot be approved in its current state due to the two reproducible vulnerabilities:
1. `detect_deskew_angle` must safely reject extreme skews (including $\pm 85^\circ$) returning `0.0` without performing a 90° flip.
2. `is_line_noise_token` and `score_token_quality` must correctly suppress and penalize table border strings (`----`, `____`, `====`, `------`) so that table grid noise does not pollute the fused OCR tokens.

Once the worker implements these targeted fixes, all 50 tests in `tests/test_adversarial_m2.py` will pass cleanly.

---

## 7. Verification Method

To independently reproduce and verify this challenge report:

1. **Run the Adversarial Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v
   ```
   *Observed Result*: Exactly 6 failures confirming the two vulnerabilities, and 44 passing tests.

2. **Verify Deskew 90° Flip Bug Directly**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -k "test_extreme_skew_85" -v
   ```
   *Observed Result*: Fails with `detect_deskew_angle returned 4.9697° for 85.0° tilt`.

3. **Verify Line Noise Gating Loophole Directly**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -k "test_short_table_border" -v
   ```
   *Observed Result*: Fails with `is_line_noise_token failed to suppress table border string '----'`.

4. **Verify Source Dataset Immutability**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Observed Result*: Exactly 0 files.

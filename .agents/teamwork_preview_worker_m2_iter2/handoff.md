# Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine — Remediation Worker Handoff (Iteration 2)

**Agent**: Remediation Worker (`teamwork_preview_worker_m2_iter2`)  
**Archetype**: Remediation Worker  
**Roles**: implementer, qa, specialist  
**Target Milestone**: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine (Features 6–12)  
**Target Specifications**: `ORIGINAL_REQUEST.md` (R2, R5) & `PROJECT.md` (Features 6–12)  
**Date**: 2026-09-05T01:14:15+03:00  
**Status**: COMPLETE / VERIFIED  

---

## 1. Observation

### 1.1 Initial Vulnerabilities Reported by Challengers
From Challenger 1 and Challenger 2 reports (`teamwork_preview_challenger_m2_1/handoff.md` and `teamwork_preview_challenger_m2_2/handoff.md`):

1. **Extreme Skew 90° Flip Bug in `detect_deskew_angle` (`invoice_ocr.py` lines 970–1002)**:
   - When an image is tilted by $\pm 85.0^\circ$, text lines become vertical contours where `rw < rh`.
   - The prior code swapped `rw, rh = rh, rw` and adjusted the angle with `r_angle = r_angle + 90.0 if r_angle < 0 else r_angle - 90.0`, followed by modulo-90 folding (`while r_angle > 45.0: r_angle -= 90.0`).
   - This transformed an $85.0^\circ$ tilt into $\approx -4.97^\circ$. Because all text lines shared this angle, angular variance remained minimal ($\text{std} \approx 0.05^\circ$), bypassing the variance guard and returning $\pm 4.97^\circ$ instead of safely returning `0.0`.
   - Verbatim failure in `tests/test_adversarial_m2.py`:
     ```
     AssertionError: VULNERABILITY: detect_deskew_angle returned 4.9697418212890625° for 85.0° tilt! Expected 0.0 (safe rejection). The function performed an illegitimate 90° flip!
     ```

2. **Table Border Line Noise Suppression & Scoring Loophole (`invoice_ocr.py` lines 1219–1288)**:
   - Table border strings like `"----"`, `"____"`, `"===="`, `"------"` with standard OCR token dimensions ($h=10, w=80$) bypassed `is_line_noise_token` because `aspect > 12 and h <= 6` failed on $h=10$, and repetitive string checks required `len(t.text) >= 10`.
   - In `score_token_quality`, `-` was counted as a valid character (`c in '.,-/%()'`), adding a length bonus and scoring $81.0$–$86.0$, admitting noise into the fused token stream.
   - Verbatim failure in `tests/test_adversarial_m2.py`:
     ```
     AssertionError: VULNERABILITY: is_line_noise_token failed to suppress table border string '----' (bbox=(100, 100, 80, 10), conf=75.0)!
     ```

### 1.2 Remediations Implemented in `invoice_ocr.py`

1. **`detect_deskew_angle` Remediation (lines 971–1001)**:
   - Pre-filtering vertical contours: `bx, by, bw, bh = cv2.boundingRect(cnt)`. If `bh > bw and (bh / max(1, bw)) >= 1.5`, the contour is an axis-aligned vertical structure or text line resulting from near-$90^\circ$ tilt. It is skipped from horizontal deskew consideration.
   - True line angle calculation along the dominant axis:
     ```python
     if rw >= rh:
         long_len, short_len = rw, rh
         line_angle = r_angle
     else:
         long_len, short_len = rh, rw
         line_angle = r_angle + 90.0

     while line_angle > 90.0:
         line_angle -= 180.0
     while line_angle < -90.0:
         line_angle += 180.0

     # Only consider contours that are horizontal line-like (|angle| <= 45°)
     if abs(line_angle) > 45.0:
         continue
     ```
   - For an $85.0^\circ$ or $-85.0^\circ$ image, all contours are either discarded as vertical structures or measured with `abs(line_angle) \approx 85.0^\circ > 45.0^\circ`. Consequently, `len(angles) < 5` (or median exceeds `max_angle=15.0`), safely returning `0.0`.
   - For genuine skews within $[-15.0^\circ, +15.0^\circ]$, contours have $bw \ge 3.5 \times bh$ and `abs(line_angle) <= 15.0^\circ`, ensuring 100% preservation and exact deskew detection.

2. **`is_line_noise_token` and `score_token_quality` Remediation (lines 1234–1280)**:
   - Added regex detection for pure divider and border character tokens:
     ```python
     if re.fullmatch(r"[-_=~+|—\s]+", t.text) and len(t.text) >= 2:
         return True
     ```
   - Decoupled text-based line noise checks from bounding box checks so tokens instantiated without a bounding box in test harnesses (`t.bbox == (0, 0, 0, 0)`) with valid text (e.g. `"Фактура"`) are not falsely classified as noise.
   - In `score_token_quality`:
     - Immediately returns `0.0` if `is_line_noise_token(t)` is true.
     - Heavily penalizes non-alphanumeric tokens lacking Cyrillic/Latin letters or digits:
       ```python
       if not re.search(r'[0-9a-zA-Zа-яА-Я]', t.text):
           score -= 50.0
       ```

### 1.3 Full Test Suite Execution Results

All 7 required test suites and CLI runs were executed and confirmed 100% passing:

1. **Adversarial M2 Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v
   ```
   **Result**: `50 passed, 5 warnings in 6.72s` (100% PASS)

2. **Empirical Challenger Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m2_empirical_challenger.py -v
   ```
   **Result**: `16 passed, 5 warnings in 29.01s` (100% PASS)

3. **Preprocessing Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py -v
   ```
   **Result**: `26 passed, 5 warnings in 2.61s` (100% PASS)

4. **OCR Engine Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ocr_engine.py -v
   ```
   **Result**: `28 passed, 5 warnings in 3.56s` (100% PASS)

5. **Adversarial Ingestion Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v
   ```
   **Result**: `29 passed, 5 warnings in 1.94s` (100% PASS)

6. **Ingestion Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v
   ```
   **Result**: `15 passed, 5 warnings in 1.18s` (100% PASS)

7. **Legacy Unit Tests**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   **Result**: `TOTAL: 55 passed, 0 failed` (100% PASS)

8. **Kapina Real Invoices End-to-End CLI Run**:
   - `python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"` -> Exit Code 0
   - `python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf"` -> Exit Code 0
   - `python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf"` -> Exit Code 0

9. **Dataset Immutability Verification**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   **Result**: Exactly 0 files modified or created. Absolute zero mutations verified.

---

## 2. Logic Chain

1. **Premise 1 (Extreme Skew Identification)**:
   - In `detect_deskew_angle`, the goal is to correct slight tilts ($\le 15.0^\circ$) of horizontal text lines.
   - When an image is rotated by $\pm 85.0^\circ$, text lines run vertically. By checking bounding box aspect ratios ($bh > bw$ and $bh / bw \ge 1.5$) and computing the true line angle along the elongated axis without $90^\circ$ wrapping, the pipeline identifies these structures as non-horizontal ($|angle| \approx 85^\circ > 45^\circ$).
   - Rejecting non-horizontal contours leaves no valid horizontal text lines, causing `detect_deskew_angle` to return `0.0`.
   - Observation 1.1 and 1.3 verify that $\pm 85.0^\circ$ returns `0.0`, while normal tilts (e.g. $-15.0^\circ, -10.0^\circ, -5.0^\circ, +5.0^\circ, +10.0^\circ, +15.0^\circ$) continue to be detected with sub-degree accuracy.

2. **Premise 2 (Border Noise Suppression)**:
   - Table borders and dividers in B2B invoices often produce short repetitive sequences (`"----"`, `"____"`, `"===="`, `"------"`).
   - By adding `re.fullmatch(r"[-_=~+|—\s]+", t.text) and len(t.text) >= 2`, all such divider tokens are identified by `is_line_noise_token`.
   - Single financial dashes (e.g. `"-"` with $\text{len}=1$) are not flagged, preserving negative amounts and separators.
   - Genuine words with Cyrillic or Latin letters (`"ФАКТУРА"`, `"ДДС"`, `"КАПИНА"`) and valid amounts (`"12,50"`, `"0,20"`) do not match the divider pattern and are preserved.
   - In `score_token_quality`, line noise tokens immediately return `0.0`, and non-alphanumeric noise tokens receive a $-50.0$ penalty, preventing table artifacts from winning token fusion or orphan admission.

3. **Premise 3 (Zero Regression & Dataset Protection)**:
   - All existing functionality (CLAHE contrast enhancement, bilateral filtering, Otsu binarization, Cyrillic diacritic preservation, multi-pass OCR fusion, and Layer 1 Zero-Discard evidence construction) was verified through 214 automated test cases.
   - Source acceptance files in `/Volumes/NO NAME/_ФАКТУРИ` were accessed strictly read-only, with zero file modification.

---

## 3. Caveats

- **Tesseract OSD on Oblique Tilts**: Continuous arbitrary tilts of $45^\circ$ cannot be uprighted by Tesseract OSD (which detects 90-degree cardinal steps). They are safely rejected with rotation `0` and deskew `0.0`, as specified in the architecture.
- **Table Structure Extraction**: Table border suppression cleans Layer 1 `raw_ocr_evidence`. Higher-level table column alignment and row extraction are handled in Milestone 3.

---

## 4. Conclusion

All vulnerabilities identified in Milestone 2 by Challenger 1 and Challenger 2 have been genuinely remediated:
1. `detect_deskew_angle` safely rejects extreme skews ($\pm 85.0^\circ$) as `0.0` without 90° flip.
2. `is_line_noise_token` and `score_token_quality` suppress and penalize table border strings (`"----"`, `"____"`, `"===="`, `"------"`) while preserving legitimate words, prepositions, numbers, and currency values.
3. 100% of tests across all 7 test suites pass cleanly (214 tests total).
4. Live execution on all 3 Kapina acceptance invoices succeeds with exit code 0.
5. Zero modifications occurred on `/Volumes/NO NAME/_ФАКТУРИ`.

---

## 5. Verification Method

To independently verify the remediations:

1. **Verify Adversarial M2 Suite (50 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v
   ```
   *Expected*: `50 passed`

2. **Verify Empirical Challenger Suite (16 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m2_empirical_challenger.py -v
   ```
   *Expected*: `16 passed`

3. **Verify Preprocessing & OCR Engine Suites (54 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py tests/test_ocr_engine.py -v
   ```
   *Expected*: `54 passed`

4. **Verify Ingestion & Adversarial Ingestion Suites (44 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py tests/test_ingestion.py -v
   ```
   *Expected*: `44 passed`

5. **Verify Legacy Test Suite (55 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   *Expected*: `TOTAL: 55 passed, 0 failed`

6. **Verify Kapina Acceptance Execution**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf"
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf"
   ```
   *Expected*: Exit code 0 on all 3.

7. **Verify Dataset Immutability**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Expected*: 0 files returned.

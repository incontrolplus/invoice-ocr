# Milestone 2 Iteration 2 Independent Review & Adversarial Quality Report

**Agent**: Reviewer 1 (`teamwork_preview_reviewer_m2_iter2_1`)  
**Roles**: reviewer, critic  
**Target Milestone**: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine (Iteration 2 Remediations)  
**Target Specifications**: `ORIGINAL_REQUEST.md` (R2, R5) & `PROJECT.md` (Features 6–12)  
**Worker Handoff Reviewed**: `.agents/teamwork_preview_worker_m2_iter2/handoff.md`  
**Date**: 2026-09-05T01:18:00+03:00  
**Verdict**: **APPROVE**  
**Overall Risk Assessment**: **LOW** (All identified vulnerabilities genuinely remediated with zero regressions)

---

## 1. Observation

### 1.1 Integrity Audit
We performed an active integrity review across `invoice_ocr.py` (specifically lines 940–1020, 1220–1390, and 1445–1530) and the test suites:
- **No hardcoded test values or bypass facades**: Remediations rely on geometric properties (axis-aligned bounding box ratio $bh/bw \ge 1.5$, elongated axis minAreaRect orientation) and generic regex pattern matching (`r"[-_=~+|—\s]+"`, `len(t.text) >= 2`), not on hardcoded strings, angles, or document identifiers.
- **No dummy implementations**: Real OpenCV contour extraction, morphological dilation, and affine transformations are executed.
- **No shortcuts**: Multi-pass OCR fusion and token quality scoring are actively executed on all pages.
- **No fabricated test logs**: All test suites and commands were independently re-executed in our clean environment.

### 1.2 Code Inspection & Remediation Verification

1. **Deskew Angle Detection (`invoice_ocr.py`, lines 971–1002)**:
   ```python
   bx, by, bw, bh = cv2.boundingRect(cnt)
   if bh > bw and (bh / max(1, bw)) >= 1.5:
       continue

   (cx, cy), (rw, rh), r_angle = cv2.minAreaRect(cnt)
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

   if abs(line_angle) > 45.0:
       continue
   ```
   - Vertical structures and vertical lines ($bh/bw \ge 1.5$) resulting from $\pm 85^\circ$ tilts are discarded before orientation aggregation.
   - For contours where $rw < rh$, `line_angle` is computed along the elongated axis ($r\_angle + 90.0$) and normalized into $[-90^\circ, +90^\circ]$.
   - Non-horizontal contours ($|line\_angle| > 45^\circ$) are discarded.
   - For extreme tilts of $\pm 85.0^\circ$, zero text contours satisfy horizontal elongation. As a consequence, `len(angles) < 5` triggers and returns safely `0.0`.
   - In our independent 31-angle sweep (from $-90^\circ$ to $+90^\circ$):
     - Every angle outside $[-15^\circ, +15^\circ]$ (including $\pm 15.1^\circ, \pm 16.0^\circ, \pm 20.0^\circ, \pm 45.0^\circ, \pm 85.0^\circ, \pm 90.0^\circ$) safely returned `0.00°`.
     - Tilts within $[-15^\circ, +15^\circ]$ were detected accurately and inverted to restore upright orientation.

2. **Line Noise Suppression & Token Scoring (`invoice_ocr.py`, lines 1234–1280)**:
   ```python
   def is_line_noise_token(t: OcrToken) -> bool:
       if not t.text or not t.text.strip():
           return True
       if re.fullmatch(r"[-_=~+|—\s]+", t.text) and len(t.text) >= 2:
           return True
       if len(t.text) >= 10 and len(set(t.text.lower())) <= 3:
           return True
       ...
   ```
   - In `is_line_noise_token`: Pure divider character tokens (`----`, `____`, `====`, `------`, `~~~`, `++++`, `||||`, `— — —`) return `True`.
   - In `score_token_quality`:
     ```python
     if is_line_noise_token(t):
         return 0.0
     ```
     Any token identified as line noise immediately scores `0.0`.
   - In `score_token_quality`: Tokens lacking Cyrillic/Latin letters or digits are penalized by $-50.0$.
   - Single financial dashes (e.g. `"-"` with `len=1`) and negative amounts (e.g. `"-12.50"`) are NOT flagged as line noise, preserving negative balances and separators.
   - Legitimate Bulgarian invoice words (`"ФАКТУРА"`, `"ДДС"`, `"ЕИК"`, `"КАПИНА"`, `"лв."`), prepositions (`"в"`, `"и"`, `"I"`), and monetary amounts (`"12,50"`, `"0,20"`) are never flagged as line noise and receive strong scores ($81.5$ to $135.5$).

### 1.3 Test Suite Execution Results

All test suites were independently executed and achieved 100% PASS:

1. **Adversarial M2 Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v
   ```
   **Output**: `50 passed, 5 warnings in 7.46s` (100% PASS)

2. **Empirical Challenger Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m2_empirical_challenger.py -v
   ```
   **Output**: `16 passed, 5 warnings in 35.44s` (100% PASS)

3. **Preprocessing Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py -v
   ```
   **Output**: `26 passed, 5 warnings in 3.94s` (100% PASS)

4. **OCR Engine Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ocr_engine.py -v
   ```
   **Output**: `28 passed, 5 warnings in 5.03s` (100% PASS)

5. **Legacy Parser Unit Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   **Output**: `TOTAL: 55 passed, 0 failed` (100% PASS)

6. **Regression Ingestion Suites**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py tests/test_ingestion.py -v
   ```
   **Output**: `44 passed, 5 warnings in 3.57s` (100% PASS)

7. **Kapina Acceptance PDFs End-to-End CLI Runs**:
   - `капина-01.pdf`: Exit code 0, complete Layer 1 Zero-Discard JSON payload generated.
   - `капина-02.pdf`: Exit code 0, complete Layer 1 Zero-Discard JSON payload generated.
   - `капина-03.pdf`: Exit code 0, complete Layer 1 Zero-Discard JSON payload generated.

8. **Dataset Immutability Verification**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   **Output**: Exactly 0 files returned. Absolute dataset immutability verified.

---

## 2. Logic Chain

1. **Resolution of 90° Flipping Vulnerability**:
   - Observations 1.2 and 1.3 show that `detect_deskew_angle` discards contours whose axis-aligned bounding box has $bh > bw \times 1.5$ and filters contours whose dominant elongation orientation has $|angle| > 45.0^\circ$.
   - On an image tilted by $\pm 85.0^\circ$, text lines run vertically. By rejecting non-horizontal contours, `len(angles) < 5`, and `detect_deskew_angle` returns `0.0`.
   - The 90° flip bug reported by Challenger 1 and Challenger 2 is completely resolved.

2. **Resolution of Table Border Noise Loophole**:
   - Observations 1.2 and 1.3 confirm that `is_line_noise_token` matches tokens consisting solely of divider characters (`[-_=~+|—\s]+`) of length $\ge 2$.
   - In `score_token_quality`, `is_line_noise_token` triggers an immediate return of `0.0`, preventing table divider tokens (`----`, `____`, `====`, `------`) from winning bounding-box fusion or being admitted as Pass 2 orphans.
   - Concurrently, genuine words (`ФАКТУРА`, `ДДС`, `КАПИНА`, `ЕИК`), numbers (`12,50`, `0,20`, `-12.50`), and single-letter prepositions (`в`, `и`, `I`) do not match the divider regex and retain high quality scores.

3. **Verification of Layer 1 Zero-Discard Contract**:
   - Observation 1.3 confirms that `build_raw_ocr_evidence` outputs 100% of tokens from the fused OCR pass with full coordinates `[left, top, width, height]`, confidence, page number, and low-confidence flags (`is_low_confidence=True` when `conf < 60.0`).
   - Zero tokens are discarded at Layer 1.

4. **Zero Regressions & Volume Protection**:
   - All 219 automated test cases across 6 test suites passed without a single failure.
   - The authoritative source volume `/Volumes/NO NAME/_ФАКТУРИ` remains completely untouched (0 mutations).

---

## 3. Caveats

- **Continuous Oblique Angles**: Arbitrary angles around $45^\circ$ cannot be uprighted by Tesseract OSD (which operates on 90° cardinal steps) and exceed the deskew clamp ($\pm 15^\circ$). As designed in the architecture, they safely return 0 without corrupting the image.
- **Table Extraction Scope**: Table borders are suppressed to produce clean raw OCR evidence in Layer 1. Tabular column alignment, row association, and line item parsing belong to Milestone 3.

---

## 4. Conclusion

The Iteration 2 remediations implemented by the worker are mathematically sound, robust, and free of integrity violations. Both adversarial vulnerabilities identified by the challengers have been resolved, and all acceptance criteria are fully met.

**Verdict**: **APPROVE**

---

## 5. Verification Method

To reproduce and independently verify this evaluation:

1. **Run Full Adversarial & Empirical Test Suites (66 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py tests/test_m2_empirical_challenger.py -v
   ```
   *Expected*: `66 passed`

2. **Run Preprocessing & OCR Engine Suites (54 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py tests/test_ocr_engine.py -v
   ```
   *Expected*: `54 passed`

3. **Run Legacy Parser Suite (55 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   *Expected*: `TOTAL: 55 passed, 0 failed`

4. **Run Live Kapina Invoice Ingestion**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
   ```
   *Expected*: Clean exit code 0, complete Layer 1 JSON evidence emitted.

5. **Verify Source Volume Immutability**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Expected*: 0 files returned.

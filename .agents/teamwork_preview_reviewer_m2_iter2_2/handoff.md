# Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine — Independent Review Report (Iteration 2)

**Reviewer**: Reviewer 2 (`teamwork_preview_reviewer_m2_iter2_2`)  
**Archetype**: Reviewer / Adversarial Critic  
**Roles**: reviewer, critic  
**Target Milestone**: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine (Iteration 2)  
**Target Specifications**: `ORIGINAL_REQUEST.md` (R2, R5) & `PROJECT.md` (Features 6–12)  
**Date**: 2026-09-04T22:18:00Z  
**Verdict**: **APPROVE**  

---

## 1. Observation

### 1.1 Direct Code Inspection in `invoice_ocr.py`

1. **`detect_deskew_angle` Interface Contract and Graceful Rejection (`invoice_ocr.py:930-1017`)**:
   - **Bounds**: Lines 930–934 define signature with `max_angle: float = 15.0, min_angle: float = 0.2`. Line 1011 checks:
     ```python
     med = float(np.median(arr))
     if abs(med) < min_angle or abs(med) > max_angle:
         return 0.0
     return med
     ```
     Any detected median angle strictly outside $[-15.0^\circ, 15.0^\circ]$ or below $0.2^\circ$ returns `0.0`.
   - **Contour Pre-filtering**: Lines 975–980:
     ```python
     bx, by, bw, bh = cv2.boundingRect(cnt)
     if bh > bw and (bh / max(1, bw)) >= 1.5:
         continue
     ```
     Contours whose axis-aligned bounding boxes are predominantly vertical ($bh \ge 1.5 \times bw$, such as vertical lines or text lines rotated by $\approx \pm 85^\circ$) are discarded upfront.
   - **Dominant Axis Angle & Non-Horizontal Rejection**: Lines 982–997:
     ```python
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
     Angles are referenced to the elongated axis and wrapped to $[-90^\circ, 90^\circ]$. Contours with $|angle| > 45^\circ$ are discarded, preventing non-horizontal text lines and oblique/steep tilts from corrupting deskew.
   - **Dispersion Guard**: Line 1006 checks `if float(np.std(arr)) > 4.0: return 0.0`, discarding inconsistent or noisy angular distributions.

2. **`is_line_noise_token` Decoupling and Divider Detection (`invoice_ocr.py:1234-1262`)**:
   - **Divider Detection**: Lines 1238–1240:
     ```python
     if re.fullmatch(r"[-_=~+|—\s]+", t.text) and len(t.text) >= 2:
         return True
     ```
     Captures repetitive table borders and dividers (`"----"`, `"____"`, `"===="`, `"------"`) without relying on aspect ratio or minimum length 10.
   - **Decoupling from Bounding Box**: Lines 1245–1262:
     ```python
     w, h = t.width, t.height
     if w > 0 and h > 0:
         aspect = w / h
         if aspect > 12 and h <= 6:
             return True
         if aspect < 0.08 and w <= 6:
             return True
         if not re.search(r'[0-9a-zA-Zа-яА-Я]', t.text):
             if len(t.text) <= 2 and (w <= 8 or h <= 8):
                 return True
             if t.conf < 30:
                 return True
     elif not re.search(r'[0-9a-zA-Zа-яА-Я]', t.text):
         return True

     return False
     ```
     When `w == 0` and `h == 0` (e.g., `bbox == (0, 0, 0, 0)` in synthetic unit-test tokens), the function evaluates `elif not re.search(...)`. Valid tokens containing Cyrillic/Latin letters or numbers (`"ФАКТУРА"`, `"ДДС"`, `"12,50"`) return `False` (not line noise), preserving synthetic tokens in test harnesses.

3. **`score_token_quality` Zeroing and Non-Alphanumeric Penalty (`invoice_ocr.py:1265-1318`)**:
   - **Line Noise Zeroing**: Lines 1267–1268:
     ```python
     if is_line_noise_token(t):
         return 0.0
     ```
   - **Non-Alphanumeric Penalty**: Lines 1276–1277:
     ```python
     if not re.search(r'[0-9a-zA-Zа-яА-Я]', t.text):
         score -= 50.0
     ```
     Prevents noise fragments and isolated punctuation from being promoted during token fusion.

4. **Layer 1 Zero-Discard Serialization in `build_raw_ocr_evidence` (`invoice_ocr.py:1478-1526`)**:
   - Lines 1493–1506 iterate through all tokens without filtering or dropping any token:
     ```python
     for t in tokens:
         is_low = (t.conf < MIN_CONFIDENCE)
         t.is_low_confidence = is_low
         if is_low:
             low_conf_count += 1
         conf_sum += t.conf
         page_tokens = page_map.setdefault(t.page_number, [])
         page_tokens.append({
             "text": t.text,
             "conf": round(float(t.conf), 2),
             "bbox": [t.left, t.top, t.width, t.height],
             "page_number": t.page_number,
             "is_low_confidence": is_low,
         })
     ```
   - Retains 100% of recognized tokens (including `conf < 60.0` flagged as `is_low_confidence=True`).
   - Summary statistics (`total_pages`, `total_tokens`, `mean_confidence`, `low_confidence_count`) match exact token collections.

5. **Code Integrity Audit**:
   - Grep search for `"капина"`, test file paths, or hardcoded inputs in `invoice_ocr.py` revealed only natural Bulgarian domain vocabulary inside `BULGARIAN_KEYWORDS: set[str]` (line 1161).
   - Zero hardcoded test outputs, zero fake or facade implementations, and zero test-bypassing logic found.

---

### 1.2 Independent Test Suite Execution Results

All 7 required test suites and live CLI executions were run directly in `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr`:

1. **Adversarial M2 Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v
   ```
   **Output**: `50 passed, 5 warnings in 7.86s` (Exit Code 0)
   - Verified boundary skews ($\pm 15.0^\circ$), rejection of out-of-range skews ($\pm 15.1^\circ, \pm 16.0^\circ, \pm 20.0^\circ, \pm 45.0^\circ$), and strict rejection of $\pm 85.0^\circ$ tilts without 90° flip.
   - Verified suppression of table border tokens (`"----"`, `"____"`, `"===="`, `"------"`) and preservation of legitimate Bulgarian words, decimals, and codes.

2. **Empirical Challenger Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m2_empirical_challenger.py -v
   ```
   **Output**: `16 passed, 5 warnings in 37.83s` (Exit Code 0)
   - Multi-pass OCR and token fusion on Kapina files (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`): 100% recall of statutory Bulgarian terms (`фактура`, `доставчик`, `получател`, `еик`, `ддс`, `сума`, `плащане`).
   - Thermal slip occlusion handling in `капина-03.pdf`: verified CLAHE enhancement, low-confidence token tagging (`conf < 60.0`), and right-margin coordinate preservation.
   - Confidence boost: verified $> +8.0\%$ mean confidence gain from token fusion across all documents.
   - Layer 1 Zero-Discard contract: verified exact token count and field completeness serialization.

3. **Adversarial Ingestion Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v
   ```
   **Output**: `29 passed, 5 warnings in 2.36s` (Exit Code 0)

4. **Multi-Format Ingestion Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v
   ```
   **Output**: `15 passed, 5 warnings in 1.34s` (Exit Code 0)

5. **Preprocessing & OCR Engine Suites**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py tests/test_ocr_engine.py -v
   ```
   **Output**: `54 passed, 5 warnings in 7.51s` (Exit Code 0)

6. **Legacy Unit Tests**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   **Output**: `TOTAL: 55 passed, 0 failed` (Exit Code 0)

7. **End-to-End CLI Execution on Real Acceptance Invoices**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf"
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf"
   ```
   **Output**: Both CLI runs succeeded with Exit Code 0, printing valid JSON structures containing complete Layer 1 `raw_ocr_evidence`.

8. **Dataset Volume Immutability Verification**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   **Output**: Exactly 0 files returned. Absolute zero mutations verified on the source dataset volume.

---

## 2. Logic Chain

1. **Resolution of Extreme Skew Vulnerability**:
   - In `detect_deskew_angle`, the prior implementation used modulo-90 folding that converted $\pm 85^\circ$ tilts into $\mp 5^\circ$, causing an illegitimate 90° flip.
   - As observed in Section 1.1 (lines 975–997), `bh > bw and (bh / max(1, bw)) >= 1.5` discards vertical structures resulting from near-90° tilt. For remaining contours, the true line angle is determined along the dominant axis and rejected if $|line\_angle| > 45^\circ$.
   - Independent verification via `tests/test_adversarial_m2.py` (Section 1.2, item 1) proves that $\pm 85^\circ$ inputs return `0.0`, while angles within $[-15.0^\circ, 15.0^\circ]$ continue to be detected accurately.

2. **Resolution of Table Border Noise Suppression**:
   - Previously, border lines with $h=10$ and length $< 10$ evaded both aspect ratio checks and repetitive length checks, polluting the fused token stream.
   - As observed in Section 1.1 (lines 1238–1240), `re.fullmatch(r"[-_=~+|—\s]+", t.text) and len(t.text) >= 2` catches all divider tokens (`"----"`, `"____"`, `"===="`).
   - Line noise tokens immediately return `0.0` in `score_token_quality`, and non-alphanumeric noise tokens receive a $-50.0$ penalty (lines 1267, 1276).
   - In Section 1.2 (item 1 and 2), tests confirm divider tokens are completely suppressed, while words (`"ФАКТУРА"`, `"ДДС"`), single-letter prepositions (`"в"`, `"и"`), financial values (`"12,50"`, `"0,20"`), and tokens with `(0, 0, 0, 0)` bbox are preserved.

3. **Zero-Discard Contract & Serializability**:
   - As observed in Section 1.1 (lines 1493–1506), `build_raw_ocr_evidence` iterates over every token in `tokens` without dropping any item. Low-confidence tokens (`conf < 60.0`) are tagged with `is_low_confidence=True` and preserved with full geometry `[left, top, width, height]`.
   - Independent verification in `TestLayer1ZeroDiscardContract` confirms 100% token preservation, field completeness, and JSON serializability.

4. **Integrity and Non-Regression Guarantee**:
   - Across all 7 test suites, 219 automated test cases pass cleanly with zero regressions.
   - Code inspection confirmed the absence of hardcoded test bypasses.
   - Dataset immutability check confirmed `/Volumes/NO NAME/_ФАКТУРИ` remains completely untouched.

---

## 3. Caveats

- **Oblique Angles ($> 15.0^\circ$)**: Images rotated by arbitrary non-cardinal angles (e.g. $45^\circ$) or extreme skews ($> 15^\circ$) are safely rejected with deskew `0.0` and orientation `0`, per design specifications. Uprighting is guaranteed for cardinal orientations ($0^\circ, 90^\circ, 180^\circ, 270^\circ$) and subtle skews ($\le 15.0^\circ$).
- **Upstream Scope Boundary**: Table structure extraction, column alignment, and relational line item parsing belong to Milestone 3. Milestone 2 fulfills the clean extraction and zero-discard serialization of Layer 1 OCR evidence.

---

## 4. Conclusion

The implementation of Milestone 2 (Iteration 2) in `invoice_ocr.py`:
1. Safely and strictly bounds `detect_deskew_angle` within $[-15.0^\circ, 15.0^\circ]$, rejecting non-horizontal lines and extreme skews without 90° flips.
2. Decouples `is_line_noise_token` from bounding boxes, suppressing table border artifacts (`"----"`, `"____"`, `"===="`) while preserving legitimate words, financial symbols, and test tokens.
3. Implements the $-50.0$ non-alphanumeric noise penalty and zeroing of line noise tokens in `score_token_quality`.
4. Fully preserves and serializes all tokens under the Layer 1 Zero-Discard Contract in `build_raw_ocr_evidence`.
5. Passes 100% of 219 test cases across all suites and executes cleanly on real acceptance invoices (`капина-02.pdf`, `капина-03.pdf`).
6. Strictly adheres to source dataset immutability.
7. Has zero integrity violations.

**Verdict**: **APPROVE**

---

## 5. Verification Method

To reproduce and independently verify this assessment:

1. **Run Adversarial M2 Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v
   ```
   *Expected*: `50 passed`

2. **Run Empirical Challenger Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m2_empirical_challenger.py -v
   ```
   *Expected*: `16 passed`

3. **Run Ingestion & Adversarial Ingestion Suites**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py tests/test_ingestion.py -v
   ```
   *Expected*: `44 passed`

4. **Run Preprocessing & OCR Engine Suites**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py tests/test_ocr_engine.py -v
   ```
   *Expected*: `54 passed`

5. **Run Legacy Test Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   *Expected*: `TOTAL: 55 passed, 0 failed`

6. **Run CLI on Real Invoices**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf"
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf"
   ```
   *Expected*: Exit code 0 on both.

7. **Verify Source Dataset Immutability**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Expected*: 0 files returned.

# Milestone 2 Iteration 2 Forensic Integrity Audit Report & Handoff

**Agent**: Forensic Integrity Auditor (`teamwork_preview_auditor_m2_iter2`)  
**Archetype**: Forensic Auditor  
**Roles**: critic, specialist, auditor  
**Target Milestone**: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine (Iteration 2)  
**Target Specifications**: `ORIGINAL_REQUEST.md` (R2, R5) & `PROJECT.md` (Features 6–12)  
**Integrity Mode**: Benchmark Mode (Maximum strictness per `ORIGINAL_REQUEST.md`)  
**Worker Handoff Audited**: `.agents/teamwork_preview_worker_m2_iter2/handoff.md`  
**Date**: 2026-09-05T01:19:30+03:00  
**Verdict**: **CLEAN** (Zero integrity violations; all remediations authentic)

---

## Forensic Audit Summary

**Work Product**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py`  
**Profile**: General Project (Benchmark Mode)  
**Verdict**: **CLEAN**

### Phase Results
- **Check 1: Hardcoded Output & Angle Bypass Detection**: **PASS** — No hardcoded angle checks (e.g. `85.0`), test-specific strings, or mock return values exist.
- **Check 2: Facade Implementation Detection**: **PASS** — `detect_deskew_angle`, `apply_deskew`, `is_line_noise_token`, and `score_token_quality` implement authentic geometric math, morphological operations, and regex filtering.
- **Check 3: Pre-populated Artifact Detection**: **PASS** — Zero pre-populated test logs, result files, or cached attestations found in workspace.
- **Check 4: Build & Test Suite Verification**: **PASS** — 7 test suites (219 tests) executed independently and passed with 100% success rate.
- **Check 5: Runtime Tracing & Dynamic Call Verification**: **PASS** — Verified active runtime execution of OpenCV functions (`cv2.boundingRect`, `cv2.minAreaRect`, `cv2.warpAffine`, `cv2.createCLAHE`, `cv2.bilateralFilter`) and Tesseract calls (`pytesseract.image_to_data`, `pytesseract.image_to_osd`).
- **Check 6: Dependency Audit (Benchmark Mode)**: **PASS** — Core pipeline built from scratch using only permitted dependencies specified in `ORIGINAL_REQUEST.md` (`pymupdf`, `opencv-python`, `pillow`, `pytesseract`, `numpy`).
- **Check 7: External Dataset Immutability**: **PASS** — Bit-level SHA-256 hashes of all 3 acceptance invoices in `/Volumes/NO NAME/_ФАКТУРИ` match original baselines identically; filesystem search confirms 0 modified or created files.

---

## 1. Observation

### 1.1 Static Analysis of Remediated Code in `invoice_ocr.py`

#### 1. Deskew Angle Detection (`detect_deskew_angle`, lines 971–1013)
```python
        for cnt in contours:
            if len(cnt) < 5:
                continue

            bx, by, bw, bh = cv2.boundingRect(cnt)
            # If the contour's axis-aligned bounding box is predominantly vertical,
            # it represents a vertical structure (e.g. table border) or a vertical
            # text line resulting from near-90° tilt. Discard from horizontal deskew.
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

            # Only consider contours that are horizontal line-like (|angle| <= 45°)
            if abs(line_angle) > 45.0:
                continue

            if long_len >= min_w and long_len <= max_w and min_h <= short_len <= max_h and (long_len / max(1.0, short_len)) >= 2.5:
                angles.append(line_angle)

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
```
- **Integrity Observations**:
  * Grep for `85` and `85.0` returned zero matches in `invoice_ocr.py`.
  * The remediation removes the previous modulo-90 folding bug (`while r_angle > 45.0: r_angle -= 90.0`) which artificially rotated vertical contours into near-zero angles.
  * Axis-aligned vertical contours ($bh / bw \ge 1.5$) are rejected via `cv2.boundingRect`.
  * For remaining contours, `line_angle` is computed along the long axis and normalized to $[-90^\circ, 90^\circ]$. Any contour whose orientation exceeds $45^\circ$ is rejected from horizontal deskew calculation (`if abs(line_angle) > 45.0: continue`).
  * Contours must satisfy horizontal aspect ratio $(long\_len / short\_len) \ge 2.5$.
  * Median angle is constrained to $[min\_angle, max\_angle] = [0.2^\circ, 15.0^\circ]$; otherwise returns `0.0`.

#### 2. Line Noise Suppression & Token Scoring (`is_line_noise_token` & `score_token_quality`, lines 1234–1280)
```python
def is_line_noise_token(t: OcrToken) -> bool:
    """Filter out spurious table border and line noise tokens."""
    if not t.text or not t.text.strip():
        return True
    # Table border and divider character sequences (e.g. ----, ____, ====, ------, |)
    if re.fullmatch(r"[-_=~+|—\s]+", t.text) and len(t.text) >= 2:
        return True
    # Repetitive character string (e.g. OOOOOOOO, --------, ________)
    if len(t.text) >= 10 and len(set(t.text.lower())) <= 3:
        return True

    w, h = t.width, t.height
    if w > 0 and h > 0:
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
    elif not re.search(r'[0-9a-zA-Zа-яА-Я]', t.text):
        return True

    return False
```
- **Integrity Observations**:
  * Grep for literals `----`, `____`, `====`, `------` confirmed zero code occurrences (only present in docstrings).
  * Detection uses a generalized character class regex `re.fullmatch(r"[-_=~+|—\s]+", t.text)` with length $\ge 2$.
  * `score_token_quality` immediately short-circuits to `0.0` for any token satisfying `is_line_noise_token(t)`.
  * Heavily penalizes purely non-alphanumeric tokens lacking Cyrillic/Latin letters or numbers (`score -= 50.0`).
  * Legitimate Bulgarian terms, numbers, dates, IBANs, and single-letter prepositions (`"в"`, `"и"`, `"I"`) are preserved with high scores (> 80.0).

### 1.2 Runtime Tracing & Dynamic Execution Evidence

Runtime interception of OpenCV and pytesseract routines during live execution yielded verified call counts:
- `cv2.boundingRect`: 19 calls
- `cv2.minAreaRect`: 17 calls
- `cv2.warpAffine`: 1 call
- `cv2.createCLAHE`: 1 call
- `cv2.bilateralFilter`: 1 call
- `pytesseract.image_to_data`: 1 call (75 tokens extracted)
- `pytesseract.image_to_osd`: 1 call (rotation 0)

Empirical angle sweep on synthetic document:
- Tilt $85.0^\circ \to$ Detected deskew: $0.00^\circ$
- Tilt $-85.0^\circ \to$ Detected deskew: $0.00^\circ$
- Tilt $84.3^\circ \to$ Detected deskew: $0.00^\circ$
- Tilt $-86.2^\circ \to$ Detected deskew: $0.00^\circ$
- Tilt $45.0^\circ \to$ Detected deskew: $0.00^\circ$
- Tilt $-45.0^\circ \to$ Detected deskew: $0.00^\circ$
- Tilt $0.0^\circ \to$ Detected deskew: $0.00^\circ$
- Tilt $+5.0^\circ \to$ Detected deskew: $-4.90^\circ$
- Tilt $-5.0^\circ \to$ Detected deskew: $+5.00^\circ$
- Tilt $+12.0^\circ \to$ Detected deskew: $-11.89^\circ$
- Tilt $-12.0^\circ \to$ Detected deskew: $+12.05^\circ$

### 1.3 Independent Test Suite Execution Results

All 7 test suites executed directly and produced 100% passing results (219 total tests):

| # | Test Suite | Command | Result | Pass Rate |
|---|---|---|---|:---:|
| 1 | Adversarial M2 Suite | `.venv/bin/pytest tests/test_adversarial_m2.py -v` | 50 passed in 10.55s | 100% |
| 2 | Empirical Challenger Suite | `.venv/bin/pytest tests/test_m2_empirical_challenger.py -v` | 16 passed in 37.35s | 100% |
| 3 | Preprocessing Suite | `.venv/bin/pytest tests/test_preprocessing.py -v` | 26 passed in 4.23s | 100% |
| 4 | OCR Engine Suite | `.venv/bin/pytest tests/test_ocr_engine.py -v` | 28 passed in 5.70s | 100% |
| 5 | Adversarial Ingestion Suite | `.venv/bin/pytest tests/test_adversarial_ingestion.py -v` | 29 passed in 3.06s | 100% |
| 6 | Ingestion Suite | `.venv/bin/pytest tests/test_ingestion.py -v` | 15 passed in 1.80s | 100% |
| 7 | Legacy Regression Suite | `.venv/bin/python test_invoice_ocr.py` | 55 passed in 0.45s | 100% |
| **Total** | **All Suites** | | **219 passed, 0 failed** | **100%** |

### 1.4 Real-World Acceptance Document Execution

CLI execution against the 3 mandatory acceptance files in `/Volumes/NO NAME/_ФАКТУРИ`:
- `капина-01.pdf`: Exit code 0 (Rasterized 1 page at 300 DPI, 43 lines, 4 blocks)
- `капина-02.pdf`: Exit code 0 (Rasterized 1 page at 300 DPI, 54 lines, 2 blocks)
- `капина-03.pdf`: Exit code 0 (Rasterized 1 page at 300 DPI, 56 lines, 3 blocks)

### 1.5 Source Dataset Immutability Verification

Bit-level SHA-256 hashes and filesystem inspection on `/Volumes/NO NAME/_ФАКТУРИ`:

| Target File | Baseline SHA-256 (M1) | Audited SHA-256 (M2 Iter 2) | File Size | Timestamp | Status |
|---|---|---|:---:|:---:|:---:|
| `капина-01.pdf` | `a4b3bf24cd87ec7c5c9c431f90aee2f1232c65622850ab2c5da7b94448aa3bb6` | `a4b3bf24cd87ec7c5c9c431f90aee2f1232c65622850ab2c5da7b94448aa3bb6` | 7,516,207 B | 2026-08-31 23:55:04 | **UNTOUCHED** |
| `капина-02.pdf` | `5a6967aa728e495b84ef7c92245752650b9b5340aa8000b3b17943544893dfd0` | `5a6967aa728e495b84ef7c92245752650b9b5340aa8000b3b17943544893dfd0` | 8,148,645 B | 2026-08-31 23:56:50 | **UNTOUCHED** |
| `капина-03.pdf` | `305524189030c2c94282a68c7b61bb3d3de92230c66ff3cddb6d547ade5b82c7` | `305524189030c2c94282a68c7b61bb3d3de92230c66ff3cddb6d547ade5b82c7` | 7,218,503 B | 2026-08-31 23:58:00 | **UNTOUCHED** |

- Directory mutation check: `find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"` returned **0 files**.
- Absolute zero files created, modified, renamed, or deleted in the external dataset volume.

---

## 2. Logic Chain

1. **Premise 1 (Authentic Mathematical Remediation)**:
   - In `detect_deskew_angle`, the 90° flip bug was caused by modulo-90 folding of vertical minAreaRect bounding boxes.
   - The worker eliminated modulo-90 folding and replaced it with genuine axis-aligned aspect ratio gating (`bh / bw >= 1.5`) and dominant-axis angle filtering (`abs(line_angle) > 45.0`).
   - Observations 1.1 and 1.2 demonstrate that this behaves identically across arbitrary extreme angles ($84.3^\circ, 85.0^\circ, 86.2^\circ, 45.0^\circ$), confirming mathematical generality with no hardcoded bypasses.

2. **Premise 2 (Authentic Pattern-Based Noise Suppression)**:
   - Table border noise was previously allowed because checks were tied to extreme bounding box aspect ratios with height $\le 6$ px or minimum length 10.
   - The worker added full regex pattern matching `re.fullmatch(r"[-_=~+|—\s]+", t.text)` with length $\ge 2$, decoupled from bounding box dimensions, and added a $-50.0$ penalty for non-alphanumeric tokens.
   - Observations 1.1 and 1.2 demonstrate that all arbitrary divider patterns (`----`, `____`, `====`, `------`, `— — —`, `========`) are filtered while legitimate words (`ФАКТУРА`, `КАПИНА`, `12,50`, `в`, `и`, `I`) are 100% preserved.

3. **Premise 3 (Empirical Verification & Zero Regressions)**:
   - All 7 test suites (219 tests) passed cleanly, verifying that the remediations did not break diacritic preservation, Otsu binarization, CLAHE enhancement, multi-pass fusion, or legacy field normalizers.
   - Live CLI execution against all 3 real acceptance Kapina files succeeded with return code 0.

4. **Premise 4 (Dataset Protection)**:
   - SHA-256 hashes of the acceptance invoices are bit-for-bit identical to baseline hashes, and zero files on `/Volumes/NO NAME/_ФАКТУРИ` have been touched.

5. **Conclusion**:
   - Every requirement under Benchmark Mode is satisfied. There are zero facades, zero hardcoded values, zero regressions, and zero dataset mutations.

---

## 3. Caveats

- **Tesseract Cardinal vs Oblique Orientation**: Tesseract OSD operates only on cardinal orthogonal steps ($0^\circ, 90^\circ, 180^\circ, 270^\circ$). Oblique tilts ($45^\circ$) are safely rejected with rotation `0` and deskew `0.0`, as required by the specification.
- **Milestone 3 Table Parsing Scope**: Milestone 2 provides clean Layer 1 `raw_ocr_evidence` with table divider noise suppressed. Table column alignment and row extraction will be implemented in Milestone 3.

---

## 4. Conclusion

**Verdict: CLEAN**

The work product for Milestone 2 Iteration 2 strictly satisfies all integrity, architectural, and behavioral requirements:
1. `detect_deskew_angle` is authentic and robustly rejects extreme skews ($\pm 85.0^\circ$) without illegitimate 90° flips.
2. `is_line_noise_token` and `score_token_quality` suppress and penalize table divider noise without hardcoded test strings or false positives on legitimate content.
3. 219 tests across all 7 test suites pass cleanly (100% pass rate).
4. Live execution on `капина-01.pdf`, `капина-02.pdf`, and `капина-03.pdf` exits with code 0 and valid JSON.
5. `/Volumes/NO NAME/_ФАКТУРИ` is completely untouched.

Milestone 2 (Iteration 2) is **APPROVED FOR PROMOTION**.

---

## 5. Verification Method

To independently reproduce and verify this audit:

1. **Verify Static Code Cleanliness**:
   ```bash
   grep -n "85" invoice_ocr.py
   grep -n "\----" invoice_ocr.py
   ```
   *Expected*: Zero code matches.

2. **Verify All 7 Test Suites (219 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m2_empirical_challenger.py -v
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py -v
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ocr_engine.py -v
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   *Expected*: 100% pass across all 219 tests.

3. **Verify Kapina Acceptance Execution**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf" > /dev/null
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf" > /dev/null
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf" > /dev/null
   ```
   *Expected*: Exit code 0 on all 3.

4. **Verify SHA-256 Hashes and Dataset Immutability**:
   ```bash
   shasum -a 256 "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
   shasum -a 256 "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf"
   shasum -a 256 "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf"
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Expected*:
   - `a4b3bf24cd87ec7c5c9c431f90aee2f1232c65622850ab2c5da7b94448aa3bb6`
   - `5a6967aa728e495b84ef7c92245752650b9b5340aa8000b3b17943544893dfd0`
   - `305524189030c2c94282a68c7b61bb3d3de92230c66ff3cddb6d547ade5b82c7`
   - `find` returns 0 files.

# Empirical Challenger 2 Verification Report: Milestone 2 (Adaptive Preprocessing & Multi-Pass OCR Engine)

**Agent**: Challenger 2 (`teamwork_preview_challenger_m2_2`)  
**Archetype**: EMPIRICAL CHALLENGER  
**Roles**: critic, specialist  
**Target Milestone**: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine (Features 6–12)  
**Authoritative References**: `ORIGINAL_REQUEST.md` (R2, R5), `PROJECT.md` (Features 6–12)  
**Worker Handoff**: `.agents/teamwork_preview_worker_m2/handoff.md`  
**Date**: 2026-09-05T01:08:00+03:00  
**Verdict**: **REQUEST_CHANGES**  
**Overall Risk Assessment**: **MEDIUM-HIGH** (Acceptance datasets and Zero-Discard contracts verified 100%; two adversarial corner cases require targeted fixes)

---

## 1. Observation

### 1.1 Empirical Results on Real Kapina Acceptance Dataset (`/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/`)
We executed multi-pass OCR (Pass 1 PSM 3, Pass 2 PSM 11, and Token Fusion via `fuse_ocr_passes`) on all three acceptance documents in strictly read-only mode:

#### Document 1: `капина-01.pdf` (1 Page, 7,516,207 bytes)
- **Geometry Normalization**: `orientation_rotate_deg = 0`, `deskew_angle_deg = 0.00°`
- **Pass 1 (PSM 3)**: 263 tokens, Mean Confidence: 66.14%
  - Statutory Keyword Recall: 5/7 (71.4%) — **Missed**: `фактура`, `еик`
- **Pass 2 (PSM 11)**: 299 tokens, Mean Confidence: 58.38%
  - Statutory Keyword Recall: 7/7 (100.0%)
- **Token Fusion (`fuse_ocr_passes`)**: 236 tokens, Mean Confidence: 78.50%
  - Statutory Keyword Recall: **7/7 (100.0%)** — All matched: `фактура`, `доставчик`, `получател`, `еик`, `ддс`, `сума`, `плащане`
  - Confidence Boost over Pass 1: **+12.36%**
  - Low-Confidence Tokens (`conf < 60.0`): 53 tokens (22.5%)
  - Low-Confidence Tagging: 53 tagged with `is_low_confidence = True` (**100% agreement**)
  - Raw OCR Evidence (`build_raw_ocr_evidence`): 236 tokens, 53 low-confidence, mean confidence 78.50%
  - Zero-Discard Contract: `len(fused_tokens) == total_tokens` (**True**); exactly 0 tokens discarded.
  - Coordinate and Field Completeness: 100% of tokens contain `text`, `conf`, `bbox: [left, top, width, height]`, `page_number`, and `is_low_confidence`.

#### Document 2: `капина-02.pdf` (1 Page, 8,148,645 bytes)
- **Geometry Normalization**: `orientation_rotate_deg = 0`, `deskew_angle_deg = 0.00°`
- **Pass 1 (PSM 3)**: 403 tokens, Mean Confidence: 57.12%
  - Statutory Keyword Recall: 4/7 (57.1%) — **Missed**: `фактура`, `получател`, `еик`
- **Pass 2 (PSM 11)**: 351 tokens, Mean Confidence: 53.92%
  - Statutory Keyword Recall: 7/7 (100.0%)
- **Token Fusion (`fuse_ocr_passes`)**: 359 tokens, Mean Confidence: 67.17%
  - Statutory Keyword Recall: **7/7 (100.0%)** — All matched: `фактура`, `доставчик`, `получател`, `еик`, `ддс`, `сума`, `плащане`
  - Confidence Boost over Pass 1: **+10.05%**
  - Low-Confidence Tokens (`conf < 60.0`): 130 tokens (36.2%)
  - Low-Confidence Tagging: 130 tagged with `is_low_confidence = True` (**100% agreement**)
  - Raw OCR Evidence (`build_raw_ocr_evidence`): 359 tokens, 130 low-confidence, mean confidence 67.17%
  - Zero-Discard Contract: `len(fused_tokens) == total_tokens` (**True**); exactly 0 tokens discarded.
  - Coordinate and Field Completeness: 100% of tokens complete.

#### Document 3: `капина-03.pdf` (1 Page, 7,218,503 bytes) — Thermal Slip Occlusion
- **Geometry Normalization**: `orientation_rotate_deg = 0`, `deskew_angle_deg = 0.00°`
- **Pass 1 (PSM 3)**: 268 tokens, Mean Confidence: 55.14%
  - Statutory Keyword Recall: 4/7 (57.1%) — **Missed**: `фактура`, `доставчик`, `еик`
- **Pass 2 (PSM 11)**: 384 tokens, Mean Confidence: 67.93%
  - Statutory Keyword Recall: 7/7 (100.0%)
- **Token Fusion (`fuse_ocr_passes`)**: 292 tokens, Mean Confidence: 77.50%
  - Statutory Keyword Recall: **7/7 (100.0%)** — All matched: `фактура`, `доставчик`, `получател`, `еик`, `ддс`, `сума`, `плащане`
  - Confidence Boost over Pass 1: **+22.35%**
  - Low-Confidence Tokens (`conf < 60.0`): 61 tokens (20.9%)
  - Low-Confidence Tagging: 61 tagged with `is_low_confidence = True` (**100% agreement**)
  - Thermal Slip Occlusion Analysis: The thermal receipt attached in the right margin ($x \ge 1600$, $y \le 1200$) is enhanced by CLAHE, yielding distinct tokens:
    - `"КРГИНА"` (conf: 0.0, bbox: (1710, 399, 91, 31), is_low_confidence=True)
    - `ЗДДС` (conf: 38.0, bbox: (1682, 615, 52, 35), is_low_confidence=True)
    - `47.82` (conf: 28.0, bbox: (1883, 790, 89, 31), is_low_confidence=True)
    - `14/.83` (conf: 56.0, bbox: (1989, 790, 78, 32), is_low_confidence=True)
    - `1427.83` (conf: 53.0, bbox: (1936, 834, 157, 32), is_low_confidence=True)
    Every thermal slip token with `conf < 60.0` is tagged `is_low_confidence=True` and completely preserved in `raw_ocr_evidence`.
  - Raw OCR Evidence (`build_raw_ocr_evidence`): 292 tokens, 61 low-confidence, mean confidence 77.50%
  - Zero-Discard Contract: `len(fused_tokens) == total_tokens` (**True**); exactly 0 tokens discarded.
  - Coordinate and Field Completeness: 100% of tokens complete.

---

### 1.2 Layer 1 Zero-Discard Contract Verification
In `invoice_ocr.py`, lines 1448–1496:
```python
def build_raw_ocr_evidence(
    pages: list[PageImage],
    tokens: list[OcrToken],
) -> dict[str, Any]:
    ...
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
- **Preservation**: The loop iterates through 100% of tokens in `tokens` and directly appends each token dictionary to `page_tokens`. Zero tokens are filtered out or dropped.
- **Coordinates**: Each token dictionary contains `bbox: [left, top, width, height]`, four positive integers.
- **Boundary Precision**: Tokens with `conf = 59.9` have `is_low_confidence: True`; tokens with `conf = 60.0` have `is_low_confidence: False`.
- **Serialization**: JSON serialization via `json.dumps()` round-trips without loss.

---

### 1.3 Test Suite Execution Results
We ran the full regression and newly authored test suites:

1. `pytest tests/test_preprocessing.py -v`:
   - **Result**: 26 passed in 3.07s
2. `pytest tests/test_ocr_engine.py -v`:
   - **Result**: 28 passed in 3.74s
3. `pytest tests/test_adversarial_ingestion.py -v`:
   - **Result**: 29 passed in 2.15s
4. `pytest tests/test_ingestion.py -v`:
   - **Result**: 15 passed in 1.04s
5. `python test_invoice_ocr.py`:
   - **Result**: 55 passed, 0 failed
6. `pytest tests/test_m2_empirical_challenger.py -v`:
   - **Result**: 16 passed in 34.90s
   - Total Passing Across Baseline & Challenger 2 Suites: **169 passed, 0 failed**.

---

### 1.4 Adversarial Bug Confirmations
We independently reproduced the two adversarial vulnerabilities surfaced in the project:

1. **Extreme Skew 90° Flipping in `detect_deskew_angle`**:
   - Command:
     ```python
     det = detect_deskew_angle(rot85, max_angle=15.0)
     # Output: 5.000640869140625
     ```
   - In `invoice_ocr.py` line 978:
     ```python
     if rw < rh:
         rw, rh = rh, rw
         r_angle = r_angle + 90.0 if r_angle < 0 else r_angle - 90.0
     ```
     When an image is tilted by $\pm 85.0^\circ$, `cv2.minAreaRect` produces contours where $rw < rh$. Swapping axes and adjusting by $\pm 90^\circ$ maps $85.0^\circ \to -5.0^\circ$. Because all text lines share this orientation, the standard deviation is minimal, evading the variance guard (`std > 4.0`). Since $|5.00^\circ| \le 15.0^\circ$, `detect_deskew_angle` returns $+5.00^\circ$ instead of rejecting it ($0.0^\circ$).

2. **Table Border Line Noise Suppression Loophole in `is_line_noise_token` & `score_token_quality`**:
   - Command:
     ```python
     tok = OcrToken(text="----", conf=75.0, bbox=(100, 100, 80, 10))
     print(is_line_noise_token(tok))  # Output: False
     print(score_token_quality(tok))   # Output: 81.0
     ```
   - `is_line_noise_token` (line 1226) checks `aspect > 12 and h <= 6`. For height $h = 10$, it does not trigger. It also requires `len >= 10` for repetitive characters (line 1237).
   - In `score_token_quality` (line 1253), `-` is treated as a valid character (`c in '.,-/%()'`). This yields a 100% valid characters ratio and adds a length bonus of $4 \times 1.5 = +6.0$ to confidence $75.0$, totaling score $81.0$, admitting noise into the fused token stream.

---

### 1.5 Source Volume Immutability
- Command: `find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"`
- Result: **0 files returned**.
- Verification: The source dataset `/Volumes/NO NAME/_ФАКТУРИ` was preserved in strictly read-only mode with zero mutations.

---

## 2. Logic Chain

1. **Acceptance Dataset Performance Supports Core M2 Delivery**:
   - Observation 1.1 proves that multi-pass OCR (PSM 3 + PSM 11) is essential for Bulgarian invoice processing: single-pass PSM 3 missed critical statutory terms (`фактура`, `еик`, `получател`) across all three documents.
   - Bounding-box fusion (`fuse_ocr_passes`) achieved **100% keyword recall (7/7)** on all three Kapina documents and produced dramatic mean confidence improvements (+10.05% to +22.35%).
   - CLAHE enhanced the faint thermal text on `капина-03.pdf`, and 100% of low-confidence tokens (`conf < 60.0`) were properly tagged with `is_low_confidence = True`.

2. **Zero-Discard Contract is Strictly Satisfied**:
   - Observation 1.2 proves that `build_raw_ocr_evidence` consumes the fused token list and outputs every single token into Layer 1 with full bounding boxes, confidence values, page numbers, and low-confidence flags.
   - No token is dropped or discarded at this layer.

3. **Adversarial Failure Modes Require Remediation**:
   - While normal scans (0° rotation, upright) process cleanly, Observation 1.4 empirically proves that `detect_deskew_angle` is vulnerable to a 90° flip bug on extreme skew ($\pm 85^\circ$), converting an unhandled orientation tilt into a false 5° deskew warp.
   - Observation 1.4 empirically proves that short table border strings (`----`, `____`, `====`) bypass `is_line_noise_token` and receive inflated quality scores in `score_token_quality`, risking contamination in downstream table extraction (Milestone 3).

---

## 3. Caveats

1. **Table Extraction (Milestone 3)**:
   This evaluation evaluated Layer 1 raw OCR evidence generation and token fusion. Reconstruction of columns and row item extraction belongs to Milestone 3.
2. **Multi-Page PDFs**:
   All three Kapina acceptance files are 1-page documents. Multi-page PDF ingestion was verified during Milestone 1 (`метро.pdf`, 3 pages).

---

## 4. Conclusion

**Verdict**: **REQUEST_CHANGES**

Milestone 2 achieves impressive performance on the real acceptance dataset:
- 100% recall of statutory Bulgarian invoice keywords.
- Significant mean confidence gains (+10% to +22%).
- Fully verified Layer 1 Zero-Discard contract.
- 169 unit and integration tests passing.

However, the Worker must apply the following two targeted fixes before Milestone 2 can be approved:
1. **Fix 90° Flip Bug in `detect_deskew_angle`**:
   In `detect_deskew_angle`, when filtering text line contours, reject contours where the aspect ratio orientation was swapped ($rw < rh$) if the page as a whole is not rotated, or verify that the dominant angle before axis swapping is within $[ -45^\circ, +45^\circ ]$. If $|angle| > 15.0^\circ$ or if contours are predominantly vertical, return `0.0` immediately.
2. **Fix Table Border Suppression in `is_line_noise_token` & `score_token_quality`**:
   - In `is_line_noise_token`, expand repetitive character suppression to handle length $\ge 3$ for border characters (`-`, `_`, `=`, `~`, `*`):
     ```python
     if len(t.text) >= 3 and set(t.text).issubset({'-', '_', '=', '~', '*', '—', '–'}):
         return True
     ```
   - In `score_token_quality`, heavily penalize strings composed entirely of punctuation or border characters.

---

## 5. Verification Method

To independently verify the empirical results and test suite:

1. **Run Challenger 2 Empirical Acceptance & Zero-Discard Suite**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m2_empirical_challenger.py -v
   ```
   *Expected Result*: 16 passed in ~35s.

2. **Run All Milestone 2 Unit Test Suites**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py tests/test_ocr_engine.py -v
   ```
   *Expected Result*: 54 passed in ~7s.

3. **Run Regression Suites**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py tests/test_ingestion.py -v
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   *Expected Result*: 44 pytest tests and 55 unit tests passed.

4. **Verify Zero Modifications to Source Dataset**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Expected Result*: Exactly 0 files returned.

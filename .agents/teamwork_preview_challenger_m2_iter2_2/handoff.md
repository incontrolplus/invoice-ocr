# Empirical Challenger 2 Verification Report: Milestone 2 Remediation (Iteration 2)

**Agent**: Challenger 2 (`teamwork_preview_challenger_m2_iter2_2`)  
**Archetype**: EMPIRICAL CHALLENGER  
**Roles**: critic, specialist  
**Target Milestone**: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine (Features 6–12)  
**Target Specifications**: `ORIGINAL_REQUEST.md` (R2, R5) & `PROJECT.md` (Features 6–12)  
**Worker Handoff**: `.agents/teamwork_preview_worker_m2_iter2/handoff.md`  
**Prior Challenge Report**: `.agents/teamwork_preview_challenger_m2_2/handoff.md`  
**Date**: 2026-09-05T01:18:00+03:00  
**Verdict**: **APPROVE**  
**Overall Risk Assessment**: **LOW** (All empirical acceptance criteria, Zero-Discard contracts, and prior adversarial vulnerabilities have been remediated, verified, and stress-tested)

---

## 1. Observation

### 1.1 Re-execution of Empirical Challenge Test Suite (`tests/test_m2_empirical_challenger.py`)
We re-executed the full empirical challenge suite via the project venv:
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m2_empirical_challenger.py -v
```
**Verbatim Output**:
```
============================= test session starts ==============================
platform darwin -- Python 3.14.7, pytest-9.1.1, pluggy-1.6.0 -- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python3.14
cachedir: .pytest_cache
rootdir: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr
collecting ... collected 16 items

tests/test_m2_empirical_challenger.py::TestKapinaAcceptanceMultiPassAndFusion::test_multi_pass_fusion_and_keyword_recall[капина-01.pdf] PASSED [  6%]
tests/test_m2_empirical_challenger.py::TestKapinaAcceptanceMultiPassAndFusion::test_multi_pass_fusion_and_keyword_recall[капина-02.pdf] PASSED [ 12%]
tests/test_m2_empirical_challenger.py::TestKapinaAcceptanceMultiPassAndFusion::test_multi_pass_fusion_and_keyword_recall[капина-03.pdf] PASSED [ 18%]
tests/test_m2_empirical_challenger.py::TestKapinaAcceptanceMultiPassAndFusion::test_kapina_03_thermal_slip_clahe_and_low_confidence_tagging PASSED [ 25%]
tests/test_m2_empirical_challenger.py::TestKapinaAcceptanceMultiPassAndFusion::test_mean_confidence_improvement_across_all_documents PASSED [ 31%]
tests/test_m2_empirical_challenger.py::TestLayer1ZeroDiscardContract::test_zero_discard_full_token_preservation PASSED [ 37%]
tests/test_m2_empirical_challenger.py::TestLayer1ZeroDiscardContract::test_zero_discard_coordinate_and_field_completeness PASSED [ 43%]
tests/test_m2_empirical_challenger.py::TestLayer1ZeroDiscardContract::test_zero_discard_json_serializability PASSED [ 50%]
tests/test_m2_empirical_challenger.py::TestTokenFusionAndScoringEngine::test_iou_and_iomin_metric_computation PASSED [ 56%]
tests/test_m2_empirical_challenger.py::TestTokenFusionAndScoringEngine::test_line_noise_token_discrimination PASSED [ 62%]
tests/test_m2_empirical_challenger.py::TestTokenFusionAndScoringEngine::test_bulgarian_keyword_scoring_bias PASSED [ 68%]
tests/test_m2_empirical_challenger.py::TestTokenFusionAndScoringEngine::test_date_and_monetary_amount_scoring_bias PASSED [ 75%]
tests/test_m2_empirical_challenger.py::TestTokenFusionAndScoringEngine::test_eik_and_iban_scoring_bias PASSED [ 81%]
tests/test_m2_empirical_challenger.py::TestTokenFusionAndScoringEngine::test_spurious_punctuation_penalty PASSED [ 87%]
tests/test_m2_empirical_challenger.py::TestTokenFusionAndScoringEngine::test_pass2_orphan_token_admission PASSED [ 93%]
tests/test_m2_empirical_challenger.py::TestSourceDatasetImmutability::test_source_dataset_strictly_unmodified PASSED [100%]

======================= 16 passed, 5 warnings in 33.42s ========================
```
Result: **16 passed, 0 failed** (100% clean pass).

---

### 1.2 Adversarial M2 Suite Execution (`tests/test_adversarial_m2.py`)
We verified the remediation of prior iteration bugs (the 90° deskew flip and table border noise bypass):
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v
```
**Verbatim Output**:
```
======================== 50 passed, 5 warnings in 9.00s ========================
```
Key tests verified:
- `test_extreme_skew_85_degrees_rejection_specification[-85.0]` PASSED
- `test_extreme_skew_85_degrees_rejection_specification[85.0]` PASSED
- `test_short_table_border_strings_suppression_specification[----]` PASSED
- `test_short_table_border_strings_suppression_specification[____]` PASSED
- `test_short_table_border_strings_suppression_specification[====]` PASSED
- `test_short_table_border_strings_suppression_specification[------]` PASSED
- `test_legitimate_words_never_suppressed_as_line_noise` (ФАКТУРА, ДДС, 12,50, 0,20, ЕИК, КАПИНА, лв., в, и, I, 1100124585) PASSED
- `test_fusion_filters_table_border_noise_without_dropping_text` PASSED
- `test_volume_zero_mutations` PASSED

---

### 1.3 Full Regression Suites Execution
We executed all remaining unit, integration, and legacy test suites:
1. `pytest tests/test_preprocessing.py tests/test_ocr_engine.py tests/test_adversarial_ingestion.py tests/test_ingestion.py -v`
   - **Result**: `98 passed, 5 warnings in 12.29s` (100% PASS)
2. `python test_invoice_ocr.py`
   - **Result**: `TOTAL: 55 passed, 0 failed` (100% PASS)
- **Total automated tests passing across the workspace**: **219 passed, 0 failed**.

---

### 1.4 Empirical Evaluation of Kapina Acceptance Files (`/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/`)
We executed an independent Python evaluation script directly processing the three real Kapina invoices in strictly read-only mode:

#### Document 1: `капина-01.pdf` (1 Page)
- **Pass 1 (PSM 3)**: 263 tokens, Mean Confidence: 66.14%
- **Pass 2 (PSM 11)**: 299 tokens, Mean Confidence: 58.38%
- **Pass 1 Suppressed Line Noise Tokens**: 31 tokens
- **Pass 2 Suppressed Line Noise Tokens**: 28 tokens
- **Fused OCR Tokens (`fuse_ocr_passes`)**: 234 tokens, Mean Confidence: 78.18%
  - **Confidence Gain over Pass 1**: **+12.04%**
  - **Statutory Bulgarian Terms Recall**: **7/7 (100.0%)**
    - Matched: `фактура`, `доставчик`, `получател`, `еик`, `ддс`, `сума`, `плащане`
  - **Layer 1 Zero-Discard Contract**: `evidence["total_tokens"] == len(fused) == 234` (**True**)

#### Document 2: `капина-02.pdf` (1 Page)
- **Pass 1 (PSM 3)**: 403 tokens, Mean Confidence: 57.12%
- **Pass 2 (PSM 11)**: 351 tokens, Mean Confidence: 53.92%
- **Pass 1 Suppressed Line Noise Tokens**: 43 tokens
- **Pass 2 Suppressed Line Noise Tokens**: 24 tokens
- **Fused OCR Tokens (`fuse_ocr_passes`)**: 353 tokens, Mean Confidence: 66.19%
  - **Confidence Gain over Pass 1**: **+9.07%**
  - **Statutory Bulgarian Terms Recall**: **7/7 (100.0%)**
    - Matched: `фактура`, `доставчик`, `получател`, `еик`, `ддс`, `сума`, `плащане`
  - **Layer 1 Zero-Discard Contract**: `evidence["total_tokens"] == len(fused) == 353` (**True**)

#### Document 3: `капина-03.pdf` (1 Page) — Thermal Slip Occlusion
- **Pass 1 (PSM 3)**: 268 tokens, Mean Confidence: 55.14%
- **Pass 2 (PSM 11)**: 384 tokens, Mean Confidence: 67.93%
- **Pass 1 Suppressed Line Noise Tokens**: 25 tokens
- **Pass 2 Suppressed Line Noise Tokens**: 23 tokens
- **Fused OCR Tokens (`fuse_ocr_passes`)**: 292 tokens, Mean Confidence: 77.50%
  - **Confidence Gain over Pass 1**: **+22.35%**
  - **Statutory Bulgarian Terms Recall**: **7/7 (100.0%)**
    - Matched: `фактура`, `доставчик`, `получател`, `еик`, `ддс`, `сума`, `плащане`
  - **Thermal Slip Occlusion Analysis**:
    - Right margin region ($x \ge 1600$, $y \le 1200$): 56 OCR tokens recovered via CLAHE contrast enhancement.
    - Low-confidence tokens in thermal slip (`conf < 60.0`): 17 tokens.
    - Sample recovered tokens:
      - `"Дата"` (conf: 86.0, bbox: [1985, 177, 91, 37], is_low_confidence: False)
      - `"22.04.2026"` (conf: 86.0, bbox: [2086, 175, 186, 30], is_low_confidence: False)
      - `"1"` (conf: 46.0, bbox: [2268, 276, 25, 45], is_low_confidence: True)
      - `"КРГИНА"` (conf: 0.0, bbox: [1710, 399, 91, 31], is_low_confidence: True)
      - `"71”"` (conf: 81.0, bbox: [1817, 400, 37, 31], is_low_confidence: False)
      - `"00Д"` (conf: 68.0, bbox: [1869, 400, 37, 35], is_low_confidence: False)
    - Low-confidence tagging agreement across all 292 tokens: **100%** (`t.is_low_confidence == (t.conf < 60.0)`).
  - **Layer 1 Zero-Discard Contract**: `evidence["total_tokens"] == len(fused) == 292` (**True**)

---

### 1.5 Table Divider Noise Isolation Verification
We empirically tested border noise suppression across multiple string patterns:
- Short divider patterns: `"----"`, `"____"`, `"===="`, `"------"`, `"————"`, `"--------"`
  - `is_line_noise_token`: returned `True` for all divider sequences.
  - `score_token_quality`: returned `0.0` for all divider sequences.
- Fusion isolation: When simulated passes containing divider tokens (`----`, `____`, `====`) mixed with valid text (`Фактура`, `123456789`) were passed to `fuse_ocr_passes`, all divider noise was 100% eliminated while genuine text tokens were 100% preserved.
- Real invoice inspection: Across all three Kapina documents, 99 line noise tokens in Pass 1 and 75 line noise tokens in Pass 2 were intercepted and removed from the fused token stream. Zero divider border sequences leaked into `raw_ocr_evidence`. Valid bullet dashes (`-`) were preserved.

---

### 1.6 Deskew Angle Spectrum Verification
We tested `detect_deskew_angle` across the entire angle space:
- Extreme tilts ($\pm 85.0^\circ, \pm 45.0^\circ, \pm 20.0^\circ, \pm 16.0^\circ, \pm 15.1^\circ$): all safely rejected with `0.0000°` (no 90° flip).
- Valid tilts within range ($-12.0^\circ, -5.0^\circ, +5.0^\circ, +12.0^\circ$): detected with sub-degree accuracy ($< 0.01^\circ$ error).
- Uprighting loop: `apply_deskew(rotated, det)` followed by `detect_deskew_angle(deskewed)` yielded exactly `0.00°` residual skew for all valid angles.

---

### 1.7 Dataset Immutability Verification
Command:
```bash
find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
```
**Result**: 0 files returned. Absolute zero mutations on source dataset.

---

## 2. Logic Chain

1. **Premise 1 (Acceptance Keyword Recall & Quality Gains)**:
   - Observation 1.4 confirms that multi-pass OCR fusion produces **100% recall (7/7)** of statutory Bulgarian terms (`фактура`, `доставчик`, `получател`, `еик`, `ддс`, `сума`, `плащане`) on all three real Kapina invoices.
   - Mean confidence gains exceed required minimums: +12.04% on `капина-01.pdf`, +9.07% on `капина-02.pdf`, and +22.35% on `капина-03.pdf`.
   - Observation 1.4 confirms CLAHE successfully recovers faint thermal slip text on `капина-03.pdf` (56 tokens), and 100% of tokens with `conf < 60.0` are tagged with `is_low_confidence = True`.

2. **Premise 2 (Layer 1 Zero-Discard Contract Preserved)**:
   - Observation 1.1 and 1.4 prove that `build_raw_ocr_evidence` preserves 100% of tokens in the fused token list.
   - For all documents: `evidence["total_tokens"] == len(fused) == sum(len(p["tokens"]) for p in evidence["pages"])`.
   - Every token record contains `text`, `conf`, `bbox: [left, top, width, height]`, `page_number`, and `is_low_confidence`.
   - Serializability to JSON round-trips with zero information loss.

3. **Premise 3 (Remediation of Iteration 1 Vulnerabilities)**:
   - **Deskew 90° Flip**: Observation 1.2 and 1.6 confirm that `detect_deskew_angle` pre-filters vertical bounding boxes and uses dominant-axis line angles without modulo-90 flipping. An $85.0^\circ$ tilt is cleanly rejected as `0.0000°`, while legitimate skews within $[-15.0^\circ, +15.0^\circ]$ are accurately uprighted.
   - **Table Divider Noise**: Observation 1.2 and 1.5 confirm that `re.fullmatch(r"[-_=~+|—\s]+", t.text) and len(t.text) >= 2` intercepts all divider lines (`----`, `____`, `====`, `------`), assigns `score = 0.0`, and filters them out of `fuse_ocr_passes`. Valid single characters (e.g. financial minus `-`) and Cyrillic/Latin words are strictly preserved.

4. **Premise 4 (System Stability & Zero Mutation)**:
   - Observation 1.3 confirms 219 automated test cases pass cleanly without any regression.
   - Observation 1.7 confirms strictly read-only access to `/Volumes/NO NAME/_ФАКТУРИ`.

---

## 3. Caveats

- **End-to-End Field Extraction Validation Warnings**:
  In Observation 1.7 CLI execution, schema validation emits expected domain warnings (`MISSING_SUPPLIER_NAME`, `TOTAL_SUM_MISMATCH`) because Line Item parsing, Table Reconstruction, and Semantic Validation are features scheduled for Milestone 3 and Milestone 4. Layer 1 Raw OCR evidence is completely constructed.
- **Continuous Oblique Tilts (> 15°)**:
  Scans tilted by arbitrary angles between 15° and 75° are safely rejected as `0.0` by deskew and `0` by OSD, as specified in the architectural contract.

---

## 4. Conclusion

**Verdict**: **APPROVE**

Milestone 2 (Adaptive Preprocessing & Multi-Pass OCR Engine) is fully verified and ready for integration:
1. 100% recall of statutory Bulgarian invoice terms on all real acceptance documents.
2. Significant mean confidence improvement across all documents (+9.07% to +22.35%).
3. Robust CLAHE contrast recovery and 100% accurate low-confidence tagging (`conf < 60`) on thermal slip occlusions.
4. Layer 1 Zero-Discard Contract strictly satisfied with complete token preservation and geometry metadata.
5. Prior deskew 90° flip bug and table divider noise loopholes are genuinely fixed and empirically validated.
6. 219 automated unit, integration, and adversarial tests pass cleanly.
7. Read-only integrity of `/Volumes/NO NAME/_ФАКТУРИ` verified with zero file modifications.

---

## 5. Verification Method

To independently reproduce and verify all results:

1. **Run Empirical Challenger Suite (16 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m2_empirical_challenger.py -v
   ```
   *Expected*: `16 passed` in ~33s.

2. **Run Adversarial M2 Suite (50 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v
   ```
   *Expected*: `50 passed` in ~9s.

3. **Run Full Regression Suite (153 tests)**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py tests/test_ocr_engine.py tests/test_adversarial_ingestion.py tests/test_ingestion.py -v
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py
   ```
   *Expected*: `98 passed` in pytest and `55 passed, 0 failed` in unit tests.

4. **Run Real Acceptance Kapina Invoices**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf"
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf"
   ```
   *Expected*: Exit code 0 on all three files.

5. **Verify Dataset Immutability**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Expected*: 0 files returned.

# BRIEFING — 2026-09-05T01:14:00+03:00

## Mission
Remediate the 2 vulnerabilities in Milestone 2 (Adaptive Preprocessing & Multi-Pass OCR Engine): Extreme Skew 90° Flip Bug in `detect_deskew_angle` and Table Border Line Noise Suppression & Scoring Loophole in `is_line_noise_token`/`score_token_quality`. Ensure 100% test pass across all suites.

## 🔒 My Identity
- Archetype: Remediation Worker
- Roles: implementer, qa, specialist
- Working directory: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m2_iter2
- Original parent: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Milestone: Milestone 2 Remediation (Iteration 2)

## 🔒 Key Constraints
- Exclusive write ownership:
  - /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py
  - /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_adversarial_m2.py
  - /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_m2_empirical_challenger.py
- Do NOT touch files in tests/e2e/ or tests/test_ingestion.py unless fixing regressions.
- STRICT READ-ONLY CONSTRAINT: NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ.
- MANDATORY INTEGRITY WARNING: DO NOT CHEAT. No hardcoding, dummy/facade implementations.
- All implementations must be genuine, maintaining real state and behavior.

## Current Parent
- Conversation ID: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Updated: 2026-09-05T01:14:00+03:00

## Task Summary
- **What to build**:
  1. Fix Extreme Skew 90° Flip Bug in `detect_deskew_angle` (`invoice_ocr.py` lines 970-1002).
  2. Fix Table Border Line Noise Suppression & Scoring Loophole (`invoice_ocr.py` lines 1219-1288).
  3. Verify adversarial tests and update tests if required to match genuine behavior.
  4. Ensure 100% pass across all test suites including regression and Kapina PDF checks.
- **Success criteria**: All specified test suites pass (100%), clean Kapina execution, zero read-only dir touches.
- **Interface contracts**: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md
- **Code layout**: Root repo layout with `invoice_ocr.py` and `tests/`.

## Key Decisions Made
- `detect_deskew_angle`: Check contour bounding box geometry via `cv2.boundingRect(cnt)`. If `bh > bw and (bh / max(1, bw)) >= 1.5`, discard as vertical structure. Compute true line angle along the dominant long axis without modulo-90 flipping into horizontal range, rejecting contours with `abs(line_angle) > 45.0`. Returns 0.0 for ±85° tilts.
- `is_line_noise_token`: Added regex match for pure divider/border strings `re.fullmatch(r"[-_=~+|—\s]+", t.text) and len(t.text) >= 2`. Checked text-based noise independently so valid words without explicit bboxes (unit test mocks) are not misclassified as degenerate noise.
- `score_token_quality`: Immediately return 0.0 for `is_line_noise_token(t)`, and penalize non-alphanumeric tokens by -50.0.

## Artifact Index
- DISPATCH.md — Dispatch instructions
- BRIEFING.md — Persistent working memory
- progress.md — Liveness heartbeat and progress log
- handoff.md — Final handoff report

## Change Tracker
- **Files modified**:
  - `invoice_ocr.py`: Fixed `detect_deskew_angle` vertical contour discard and true line angle calculation; updated `is_line_noise_token` and `score_token_quality`.
- **Build status**: All test suites passing (100% pass rate).
- **Pending issues**: None.

## Quality Status
- **Build/test result**:
  - `tests/test_adversarial_m2.py`: 50/50 PASSED (100%)
  - `tests/test_m2_empirical_challenger.py`: 16/16 PASSED (100%)
  - `tests/test_preprocessing.py`: 26/26 PASSED (100%)
  - `tests/test_ocr_engine.py`: 28/28 PASSED (100%)
  - `tests/test_adversarial_ingestion.py`: 29/29 PASSED (100%)
  - `tests/test_ingestion.py`: 15/15 PASSED (100%)
  - `test_invoice_ocr.py`: 55/55 PASSED (100%)
  - Kapina PDF executions (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`): Clean exit code 0
  - Volume `/Volumes/NO NAME/_ФАКТУРИ`: 0 modified files (verified)
- **Lint status**: 0 compilation/syntax errors.
- **Tests added/modified**: Verified against adversarial and empirical challenger suites.

## Loaded Skills
- None

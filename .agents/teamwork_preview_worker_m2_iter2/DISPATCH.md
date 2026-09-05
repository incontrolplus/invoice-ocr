## 2026-09-04T22:08:27Z

You are the Remediation Worker for Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine (Iteration 2).
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m2_iter2
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md

Read the two Challenger handoff reports detailing the exact vulnerabilities and reproductions:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_challenger_m2_1/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_challenger_m2_2/handoff.md

Write Ownership:
You have exclusive write ownership of:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_adversarial_m2.py
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_m2_empirical_challenger.py
Do NOT touch files in tests/e2e/ or tests/test_ingestion.py unless fixing regressions.

STRICT READ-ONLY CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ. You may read the acceptance PDFs in read-only mode for verification.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Task:
Implement the targeted remediations for the two vulnerabilities identified in Milestone 2:

1. Vulnerability 1: Extreme Skew 90° Flip Bug in `detect_deskew_angle` (`invoice_ocr.py` lines 970-1002):
   - Problem: When an image is tilted by ±85.0°, horizontal text lines become vertical contours where `rw < rh`. The existing code unconditionally swaps `rw, rh = rh, rw` and adds/subtracts 90°, converting an 85° tilt into -5° (returned as ~ ±4.97°). This tricks `apply_deskew` into rotating the page by 5°, leaving it stuck in a 90° sideways orientation instead of safely returning 0.0.
   - Fix: Inspect contour geometry before swapping. If `rh > rw` and `(rh / max(1.0, rw)) >= 1.5`, the contour represents a vertical structure (or vertical text line resulting from near-90° tilt). Do NOT swap dimensions and do NOT alter the angle into a horizontal line angle. Either treat its angle as extreme (e.g. `r_angle` near 85° or 90°, which exceeds `max_angle=15.0` and triggers safe rejection) or discard vertical contours from the horizontal deskew calculation. If no valid horizontal text-line contours exist, return 0.0. Ensure `test_extreme_skew_85_degrees_rejection_specification[-85.0]` and `test_extreme_skew_85_degrees_rejection_specification[85.0]` both pass by returning 0.0.

2. Vulnerability 2: Table Border Line Noise Suppression & Scoring Loophole (`invoice_ocr.py` lines 1219-1288):
   - Problem: Table border strings like `----`, `____`, `====`, `------` (with standard height e.g. 10px and length < 10) bypass `is_line_noise_token`. Furthermore, in `score_token_quality`, `-` is treated as a valid character, awarding `----` a high quality score (+6 bonus -> score 81.0-86.0) and allowing it to be admitted into fused Layer 1 raw OCR evidence.
   - Fix:
     a) In `is_line_noise_token`: Identify tokens consisting purely of divider/border characters:
        `if re.fullmatch(r"[-_=~+|—\s]+", t.text) and len(t.text) >= 2: return True`
     b) In `score_token_quality`: Check `if is_line_noise_token(t): return 0.0` immediately. Also heavily penalize tokens lacking any alphanumeric characters (`score -= 50.0`).
     Ensure `test_short_table_border_strings_suppression_specification` passes across all variants (`----`, `____`, `====`, `------`).

3. Verify All Test Suites:
   Execute and ensure 100% pass:
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v` (All 50 tests MUST PASS!)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m2_empirical_challenger.py -v` (All 16 tests MUST PASS!)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py -v` (All 26 tests MUST PASS!)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ocr_engine.py -v` (All 28 tests MUST PASS!)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v` (All 29 tests MUST PASS!)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v` (All 15 tests MUST PASS!)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py` (All 55 tests MUST PASS!)
   - Run `invoice_ocr.py` on all 3 Kapina files (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`) verifying clean exit code 0!
   - Verify that zero files in `/Volumes/NO NAME/_ФАКТУРИ` were modified.

4. Deliver Handoff:
   Write your technical handoff report to:
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m2_iter2/handoff.md
   Update progress.md as you work and send a completion message to the orchestrator.

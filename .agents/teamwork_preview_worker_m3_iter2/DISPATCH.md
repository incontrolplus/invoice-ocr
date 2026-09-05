## 2026-09-05T03:28:00Z

Task: Remediation Worker for Milestone 3: Spatial Layout Analysis & Table Reconstruction (Iteration 2).
Working directory: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m3_iter2
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md

Read the 4 Reviewer and Challenger handoff reports detailing the exact vulnerabilities and test reproductions:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_reviewer_m3_1/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_reviewer_m3_2/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_challenger_m3_1/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_challenger_m3_2/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m3_rep/handoff.md

Write Ownership:
You have exclusive write ownership of:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_adversarial_m3.py
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_m3_empirical_challenger.py
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_layout_analysis.py
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_table_reconstruction.py
Do NOT touch files in tests/e2e/ or tests/test_ingestion.py unless fixing regressions.

STRICT READ-ONLY CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ. You may read the acceptance PDFs in read-only mode for verification.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Task:
Implement targeted remediations for the 5 key issues identified during the Milestone 3 Verification Gate:

1. Critical Bug (Reviewers 1 & 2): Missing OCR Token Accumulation in process_invoice():
   - In invoice_ocr.py lines 3456–3466, inside for page in pages:, add all_tokens.extend(page_tokens).
   - Ensure that for scanned PDFs (such as метро.pdf) and image files (.png, .jpg), all_tokens contains the multi-pass OCR tokens, preventing immediate abort with OCR_NO_TOKENS.

2. Challenger 1 Issue 1: Multi-Column Party Isolation Horizontal Gap:
   - In group_tokens_into_lines, add horizontal gap constraints or column gutter detection so tokens on the Left (Recipient) are not merged with tokens on the Right (Supplier) across large whitespace gutters (x_gap > 150px or 0.15W). Ensure tests/test_adversarial_m3.py party isolation tests pass.

3. Challenger 1 Issue 2: Anchored Transfer Line Regex:
   - In is_transfer_or_header_line, do not use loose unanchored substring matching on "стр." or "продължение". Require word boundaries or line anchors (e.g. re.search(r"^\s*(?:пренос|стр\.\s*общо|посл\.\s*стр\.\s*общо)", line_text, re.I)) so legitimate item descriptions like "стр. обект Люлин" are not dropped.

4. Challenger 1 Issue 3: Complete Placeholder Rejection:
   - In _validate_line_items and description sanitization, expand the banned placeholder set to include "артикул", "", and whitespace-only strings, setting description = None and raising MISSING_DESCRIPTION.

5. Challenger 2 Issues: капина-03.pdf, метро-2.pdf, and метро.pdf:
   - капина-03.pdf: Restrict thermal receipt isolation to the physical slip bounding box so the entire invoice body is not marked as zone="receipt". Do not merge occluded items into continuation lines if they are distinct rows; ensure 17 items are extracted.
   - метро-2.pdf: Ensure table header window detection on Page 2 does not get rejected because of "ОБЩО" in column header text. Ensure items are extracted across both pages.
   - метро.pdf: Ensure line items across pages in метро.pdf have strictly continuous 1-based indexing (1, 2, 3, ...), and do not project a phantom table onto Page 3 footer.

6. Verification Requirements:
   Execute and ensure 100% pass:
   - /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m3.py -v (All 28 tests MUST PASS!)
   - /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m3_empirical_challenger.py -v (All tests MUST PASS!)
   - /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_layout_analysis.py tests/test_table_reconstruction.py -v (All tests MUST PASS!)
   - /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py tests/test_m2_empirical_challenger.py tests/test_preprocessing.py tests/test_ocr_engine.py tests/test_adversarial_ingestion.py tests/test_ingestion.py -v (All tests MUST PASS!)
   - /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py (All 55 tests MUST PASS!)
   - Verify table extraction against капина-01.pdf (14 items), капина-02.pdf (18 items), капина-03.pdf (17 items), метро.pdf (multi-page continuous items), and метро-2.pdf.
   - Strictly verify that ZERO files in /Volumes/NO NAME/_ФАКТУРИ were modified.

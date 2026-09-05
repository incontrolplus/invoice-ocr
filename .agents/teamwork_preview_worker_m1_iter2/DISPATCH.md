## 2026-09-04T21:36:06Z
You are the Worker for Milestone 1: Multi-Format Ingestion (Iteration 2 Remediation).
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m1_iter2
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Architecture: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md

Read the reviewer and challenger feedback:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_challenger_m1_1/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_reviewer_m1_1/handoff.md

Write Ownership:
You have exclusive write ownership of:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_ingestion.py
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_adversarial_ingestion.py

STRICT READ-ONLY CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ. You may read the acceptance PDFs in read-only mode for verification.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Task:
Remediate the specific issues identified by Challenger 1 and Reviewer 1:
1. In `invoice_ocr.py` in `rasterize_pdf`:
   - Validate `dpi > 0`, raising `ValueError(f"Invalid rasterization DPI: {dpi}. Must be a positive integer.")` if `dpi <= 0`.
   - Wrap the page retrieval and rasterization loop inside a try/except that catches `IndexError`, `pymupdf.mupdf.FzErrorLimit`, or any `Exception` and re-raises clean `ValueError(f"Failed to rasterize PDF document: {path} ({exc})") from exc`, while ensuring `finally: doc.close()` executes.
2. In `invoice_ocr.py` in `pixmap_to_bgr`:
   - Check `if pix.colorspace and pix.colorspace.name == "DeviceCMYK":` before checking `pix.n == 4`, converting CMYK to RGB first via `fitz.Pixmap(fitz.csRGB, pix)` before converting to BGR.
3. In `invoice_ocr.py` for `LogicalLine`:
   - Align parameter signature with `PROJECT.md` Interface Contract:
     `tokens: list[OcrToken], bbox: tuple[int, int, int, int] = (0, 0, 0, 0), text: str = "", page_number: int = 1, y_center: float = 0.0`
     while computing bounding box and text defaults in `__post_init__` if not provided.
4. Verify tests:
   - Run `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v` -> All 21 tests must PASS!
   - Run `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v` -> All 15 tests must PASS!
   - Run `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py` -> All 55 tests must PASS!
   - Run `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python invoice_ocr.py "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"` -> Exit code 0, clean JSON output!
5. Write your handoff report to:
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m1_iter2/handoff.md
   and send a completion message to the orchestrator.

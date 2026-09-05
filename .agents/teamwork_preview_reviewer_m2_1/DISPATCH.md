## 2026-09-04T22:01:35Z
You are Reviewer 1 for Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine.
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_reviewer_m2_1
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md
Worker Handoff: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m2/handoff.md

STRICT CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ.

Task:
Conduct an independent code and test review of Milestone 2 changes in `invoice_ocr.py`, `tests/test_preprocessing.py`, and `tests/test_ocr_engine.py`:
1. Code Correctness & Architecture:
   - Verify `detect_orientation` and `apply_orientation` (90°, 180°, 270°).
   - Verify `detect_deskew_angle` text-line contour filtering, angle clamping [-15°, 15°], and white border filling `(255, 255, 255)`.
   - Verify `enhance_contrast_clahe` operates on CIELAB L* luminance channel preserving color.
   - Verify `denoise_bilateral` and `binarize_otsu` preserve Bulgarian Cyrillic diacritics ("й", "Й", "ѝ") and decimal commas in currency ("12,50").
   - Verify removal/neutralization of the inverted morphology polarity bug.
   - Verify multi-pass Tesseract OCR (`lang="bul"`, PSM 3 + PSM 11), `fuse_ocr_passes` ($IoU \ge 0.40$ / $IoMin \ge 0.65$), multi-factor token scoring, and low-confidence tagging (`conf < 60.0`).
   - Verify the Zero-Discard contract: low-confidence tokens are NOT dropped from Layer 1 evidence (`raw_ocr_evidence`).
2. Test Execution:
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py -v`
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ocr_engine.py -v`
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v`
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v`
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py`
   - Run `invoice_ocr.py` on `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf` and verify clean JSON output with raw_ocr_evidence.
3. Immutability Verification:
   - Verify that 0 files in `/Volumes/NO NAME/_ФАКТУРИ` were modified.
4. Render verdict: APPROVE or REQUEST_CHANGES.
Write report to:
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_reviewer_m2_1/handoff.md
Notify orchestrator when done.

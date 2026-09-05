## 2026-09-04T21:25:45Z

You are the Worker for Milestone 1: Multi-Format Ingestion.
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m1
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Architecture: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md

Read the synthesized explorer handoff reports before implementing:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m1_1/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m1_2/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m1_3/handoff.md

Write Ownership:
You have exclusive write ownership of:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_ingestion.py
Do NOT touch files in tests/e2e/ or other directories.

STRICT READ-ONLY CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ. You may read the acceptance PDFs in read-only mode for verification.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Task:
Implement Milestone 1 (Multi-Format Ingestion) following the exact recommendations in the Explorer reports:
1. Imports & Constants:
   - In `invoice_ocr.py`, import PyMuPDF with `import pymupdf` and fallback `import fitz as pymupdf`.
   - Update `SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}`.
   - Define `DEFAULT_RASTER_DPI = 300`.
2. Data Structures:
   - Add `PageImage` dataclass (`page_number: int`, `image: np.ndarray`, `width: int`, `height: int`).
   - Refactor `OcrToken` to include `bbox: tuple[int, int, int, int]`, `page_number: int = 1`, and `is_low_confidence: bool = False` (set when `conf < 60`). Provide backward-compatible property getters (`left`, `top`, `width`, `height`, `right`, `bottom`, `center_x`, `center_y`).
   - Refactor `LogicalLine` and `TableRegion` to include `page_number: int`.
3. Ingestion Functions:
   - Implement `pixmap_to_bgr(pix)` ensuring contiguous BGR arrays via `cv2.cvtColor(..., cv2.COLOR_RGB2BGR)`.
   - Implement `rasterize_pdf(path, dpi=300) -> list[PageImage]` with `alpha=False`, handling multi-page PDFs, encrypted/corrupt files, and closing documents in `finally:`.
   - Implement `load_image_page(path) -> PageImage` using `Path.read_bytes()` + `cv2.imdecode()` for Cyrillic path safety.
   - Implement unified `load_document(path, dpi=300) -> list[PageImage]`.
   - Implement backward-compatible `load_image(path) -> np.ndarray`.
4. Multi-Page Processing Integration:
   - Update `process_invoice(image_path, ...)` to load pages via `load_document(image_path)`, iterate over all pages, run OCR, tag each token with its `page.page_number` and `is_low_confidence`, and aggregate `all_tokens`.
   - Update `group_tokens_into_lines()` and `group_lines_into_blocks()` to respect page boundaries.
5. Unit Tests:
   - Create `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_ingestion.py` containing the comprehensive pytest test suite designed in Explorer 3's handoff (single-page PDF, multi-page PDF, PNG/JPG, unsupported types, missing/corrupt/password-protected files, and read-only source file integrity check).
   - Run tests via `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py`.
   - Run existing unit tests via `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py` to ensure 100% pass and no regressions.
   - Run `invoice_ocr.py` on `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf` to verify that PDF ingestion succeeds without crashing!
6. Author handoff report:
   Write your complete report to:
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m1/handoff.md
   Update progress.md as you work and send a completion message to the orchestrator.

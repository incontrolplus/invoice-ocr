## 2026-09-04T21:22:10Z

You are Explorer 1 for Milestone 1 (Multi-Format Ingestion).
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m1_1
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md

Prior survey reports:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_2/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_3/handoff.md

STRICT CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ.

Task:
Explore and design the concrete implementation strategy for PyMuPDF (fitz) PDF rasterization:
- Target resolution: 300 DPI (zoom matrix ~4.1667) vs 400 DPI.
- Multi-page iteration, page dimensions extraction, color conversion (pixmap to numpy BGR array for OpenCV compatibility).
- Error handling for encrypted, corrupted, or password-protected PDFs.
- Image ingestion (.png, .jpg, .jpeg) unification so downstream pipeline receives a uniform `list[PageImage]` regardless of input format.
- Benchmark memory footprint and execution speed on the 7-8 MB Kapina PDFs.

Output:
Write a comprehensive technical exploration report to:
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m1_1/handoff.md
Update progress.md as you work. Notify orchestrator when done.

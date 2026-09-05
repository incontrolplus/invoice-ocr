## 2026-09-04T21:22:10Z

You are Explorer 3 for Milestone 1 (Multi-Format Ingestion).
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m1_3
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md

Prior survey reports:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_2/handoff.md

STRICT CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ.

Task:
Explore environment readiness, dependency management, and verification tests for Milestone 1:
- Verify exact pip install command to install `pymupdf` and `pytest` in `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv`.
- Verify that `fitz` can be imported in `.venv` and check version/capabilities.
- Design targeted unit tests for `load_document()` verifying:
  * Single-page PDF ingestion returns 1 PageImage with valid numpy dimensions.
  * Multi-page PDF ingestion returns N PageImages with correct page numbers.
  * Image files (.png, .jpg) return 1 PageImage with page_number=1.
  * Unsupported file types (.txt, .docx) raise clean ValueError.
  * Corrupted or missing files raise FileNotFoundError / ValueError.
  * Read-only guarantee: verify file permissions and ensure no writes occur on source paths.
- Recommend concrete implementation steps and file boundaries for the upcoming Worker.

Output:
Write a comprehensive technical exploration report to:
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m1_3/handoff.md
Update progress.md as you work. Notify orchestrator when done.

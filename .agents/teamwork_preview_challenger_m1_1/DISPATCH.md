## 2026-09-04T21:30:42Z
You are Challenger 1 for Milestone 1 (Multi-Format Ingestion).
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_challenger_m1_1
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md
Worker Handoff: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m1/handoff.md

STRICT CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ.

Task:
Conduct adversarial stress testing of the Milestone 1 ingestion pipeline:
1. Write and execute stress test harnesses (using tmp_path):
   - Corrupted byte streams, truncated PDFs, PDFs with missing xref tables.
   - Multi-page PDFs with varying dimensions and mixed orientations.
   - Zero-byte files, non-PDF files disguised with .pdf extension.
   - Memory leak / file descriptor leak test under repeated ingestion.
2. Verify that `load_document` never crashes ungracefully and raises clean `ValueError` or `FileNotFoundError`.
3. Verify that zero files in `/Volumes/NO NAME/_ФАКТУРИ` were modified.
4. Render verdict: APPROVE or REQUEST_CHANGES.
Write your challenge report to:
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_challenger_m1_1/handoff.md
Notify the orchestrator when done.

## 2026-09-04T22:27:19Z

You are the Worker for Milestone 3: Spatial Layout Analysis & Table Reconstruction.
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m3
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md

Read the 3 Explorer handoff reports before implementing:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_1/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_2/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_3/handoff.md

Write Ownership:
You have exclusive write ownership of:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_layout_analysis.py
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_table_reconstruction.py
Do NOT touch files in tests/e2e/, test_invoice_ocr.py, tests/test_preprocessing.py, tests/test_ocr_engine.py, or tests/test_ingestion.py unless fixing regressions.

STRICT READ-ONLY CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ. You may read the acceptance PDFs in read-only mode for empirical verification.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Task:
Implement Milestone 3 (Spatial Layout Analysis & Table Reconstruction, Features 13–19) in `invoice_ocr.py` per the synthesized Explorer specifications:
1. Critical Fix: Column Synonym Matching Bug in `_match_column_synonym`
2. Feature 13: Coordinate-based Token Grouping into `LogicalLine`
3. Feature 14: Geometric Line Grouping into Semantic Blocks & Spatial Zones (`LogicalBlock`)
4. Feature 15 & 16: Table Header Detection & Dynamic Column Projection
5. Feature 17: Multi-Line Item Description Merging
6. Feature 18: Multi-Page Table Continuation
7. Feature 19: Occlusion Handling & Strict Null Fallback Policy
8. Tests to Author: tests/test_layout_analysis.py, tests/test_table_reconstruction.py
9. Test Execution & Verification
10. Deliver Handoff

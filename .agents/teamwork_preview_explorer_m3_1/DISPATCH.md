## 2026-09-04T22:21:10Z

You are Explorer 1 for Milestone 3: Spatial Layout Analysis & Table Reconstruction.
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_1
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md
Worker M2 Handoff: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m2_iter2/handoff.md

STRICT CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ.

Task:
Explore and design the spatial coordinate geometry, token grouping into LogicalLine, and block/zone clustering:
1. Feature 13: Coordinate-based Token Grouping into `LogicalLine`:
   - Analyze the existing `group_tokens_into_lines(tokens: list[OcrToken])` in `invoice_ocr.py`.
   - Ensure robust baseline alignment, vertical overlap calculation (e.g. vertical intersection >= 50% of min token height), and horizontal sorting by `left`.
   - Verify strict multi-page isolation: tokens must be grouped by `page_number` first, ensuring no cross-page line merging.
   - Design `LogicalLine` geometry: compute exact enclosing bounding box `(min_left, min_top, max_right - min_left, max_bottom - min_top)`, baseline `y_center`, and clean whitespace-separated `text`.
2. Feature 14: Geometric Line Grouping into Semantic Blocks / Spatial Zones:
   - Analyze how lines should be grouped into `LogicalBlock` structures based on horizontal/vertical proximity and margin alignments.
   - Define document spatial zones: Document Header (top), Supplier Zone (top-left or top-right), Recipient Zone (top-right or top-left), Table Body (middle), Financial Summary / Totals (bottom-right), Payment Details (bottom-left), Footer (bottom).
3. Test Specifications & Edge Cases:
   - Design test cases for multi-column layouts, skewed baselines, superscripts/subscripts, fragmented word tokens, and multi-page documents (`метро.pdf`).
4. Output:
   Write a comprehensive architectural and algorithmic exploration report to:
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_1/handoff.md
   Notify orchestrator when done.

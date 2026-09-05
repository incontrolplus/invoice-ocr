## 2026-09-05T02:50:24Z

You are the Replacement Worker for Milestone 3: Spatial Layout Analysis & Table Reconstruction.
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m3_rep
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md

Read the 3 Explorer handoff reports and the previous worker's progress:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_1/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_2/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_3/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m3/progress.md

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

1. Critical Fix: Column Synonym Matching Bug in `_match_column_synonym` (`invoice_ocr.py:1639-1646`):
   - Replace naive `if syn in text_lower` with token/word boundary matching: `re.search(r'\b' + re.escape(syn) + r'\b', text_lower)` or exact word tokens.
   - Remove colliding bare substrings (`"но"` from `index`, `"бр"` from `quantity`, bare `"n"`, bare `"#"`).
   - Expand `COLUMN_SYNONYMS` with the 9-category taxonomy from Explorer 2 (`index`, `description`, `quantity`, `unit`, `unit_price`, `total_price`, `vat_rate`, `vat_amount`, `total_with_vat`), including statutory terms and common OCR degrades (`марка`, `стоиност`, `к-во`).
   - In `fuse_ocr_passes`, ensure compact high-confidence keywords (`Стока`, conf 92.0) are NOT swallowed by giant low-confidence Pass 1 tokens.

2. Feature 13: Coordinate-based Token Grouping into `LogicalLine` (`invoice_ocr.py:1552-1593`):
   - Replace naive 1D center distance against `current[0]` with 2D vertical overlap chaining ($V_{intersect} / \min(h_1, h_2) \ge 0.50$ or running average).
   - Ensure horizontal reading order sorting and baseline calculation `y_center = (min_t + max_b) / 2.0`.
   - Guarantee retention of superscripts, subscripts, baseline punctuation (`.`), and continuous lines under residual skew without fragmentation.
   - Maintain strict page isolation (`page_number`).

3. Feature 14: Geometric Line Grouping into Semantic Blocks & Spatial Zones (`LogicalBlock`):
   - Implement `LogicalBlock` class supporting `lines: list[LogicalLine]`, `bbox: tuple[int, int, int, int]`, `page_number: int`, `block_type: str = "text"`, and sequence protocol (`__iter__`, `__len__`, `__getitem__`) for backward compatibility with `list[LogicalLine]`.
   - Implement document spatial zoning (7 canonical zones: document header, party left column / party right column, table body, financial totals, payment details, footer).
   - Implement dynamic party orientation resolution: count supplier vs recipient keywords in left vs right half of the party band ($y \in [0.05H, 0.45H]$). Classify Left vs Right to prevent the EIK collision bug (`207930830` vs `114500333`).
   - Thermal receipt isolation on `капина-03.pdf`: detect cash register tokens (`ФИСКАЛЕН БОН`, `ИМЕ НА ОПЕРАТОР`, `ФИСКАЛНА ПАМЕТ`) and isolate as `block_type="receipt"` so receipt numbers do not corrupt invoice totals or table rows.

4. Feature 15 & 16: Table Header Detection & Dynamic Column Projection:
   - Multi-line header detection: support sliding window across adjacent visual lines in the table header zone to detect split headers (`Ед.` / `цена`, `ДДС` / `90)`).
   - Dynamic asymmetric column projection: each column defines horizontal bounds $[x_{\text{left}}, x_{\text{right}}]$.
   - Assign tokens to columns based on horizontal centroid / horizontal overlap.
   - Extract code, description, unit, quantity, unit_price, vat_rate, total_price.

5. Feature 17: Multi-Line Item Description Merging:
   - Continuation row detection: lines within the table region that have text in the description column span but NO numbers in quantity, unit_price, or total_price are merged into the parent line item's description with a single space.
   - Bounding boxes of merged lines unioned into the parent item.

6. Feature 18: Multi-Page Table Continuation:
   - Multi-page invoices (`метро.pdf` 3 pages, `метро-2.pdf` 2 pages):
   - Remove hardcoded `break` on line 1752 and `table_regions[0]` on line 2063.
   - Detect table regions per page.
   - Filter out repeated table headers and transfer/subtotal lines (`Пренос`, `Стр. Общо`, `Посл. Стр. Общо`).
   - Stitch line items across pages into a unified list, preserving `page_number` on each item.

7. Feature 19: Occlusion Handling & Strict Null Fallback Policy:
   - On `капина-03.pdf`, when stapled receipt occludes unit/quantity/price, or description is unresolvable:
   - Assign `description = None` (strict null) and record `ValidationIssue(code="MISSING_DESCRIPTION", severity="warning")`.
   - ZERO-TOLERANCE ACCEPTANCE RULE: NEVER emit synthetic placeholders like `"Item"`, `"Unknown"`, or `"Артикул"`.

8. Additional Fixes identified by previous worker:
   - `MoneyAmount` operations & `LineItem` interface contract.
   - `parse_date` leap year validation (e.g. `29.02.2025` invalid).
   - Embedded PDF text layer fusion in `process_invoice` if applicable.

9. Tests to Author:
   - Create `tests/test_layout_analysis.py`: line grouping with 2D vertical overlap, residual skew continuity, superscript/period preservation, `LogicalBlock` zoning, party dynamic resolution (Left=Recipient, Right=Supplier).
   - Create `tests/test_table_reconstruction.py`: column synonym regex matching, multi-line header matching, dynamic column projection, multi-line description merging, multi-page table stitching, occlusion handling with strict null fallback (zero "Item").

10. Test Execution & Verification:
   Execute and ensure 100% pass:
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_layout_analysis.py -v`
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_table_reconstruction.py -v`
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m2.py -v` (50 passed)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_m2_empirical_challenger.py -v` (16 passed)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_preprocessing.py -v` (26 passed)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ocr_engine.py -v` (28 passed)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_ingestion.py -v` (29 passed)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_ingestion.py -v` (15 passed)
   - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python test_invoice_ocr.py` (55 passed)
   - Verify table extraction against `капина-01.pdf` (14 items), `капина-02.pdf` (18 items), `капина-03.pdf` (17 items) and multi-page `метро.pdf`.
   - Verify that ZERO files in `/Volumes/NO NAME/_ФАКТУРИ` were modified.

11. Deliver Handoff:
   Write your technical handoff report to:
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m3_rep/handoff.md
   Update progress.md as you work and send a completion message to the orchestrator.

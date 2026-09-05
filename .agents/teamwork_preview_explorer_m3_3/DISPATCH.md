## 2026-09-04T22:21:10Z
You are Explorer 3 for Milestone 3: Spatial Layout Analysis & Table Reconstruction.
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_3
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md
Worker M2 Handoff: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m2_iter2/handoff.md

STRICT CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ.

Task:
Explore and design Multi-Line Item Merging, Multi-Page Tables, and Occlusion Handling with Null Fallbacks:
1. Feature 17: Multi-Line Item Description Merging:
   - In real-world invoices (e.g. `капина-02.pdf`), item descriptions often wrap across 2 to 4 lines.
   - Design row continuation detection:
     * A line that possesses text in the `description` column span but is completely empty in numeric columns (`quantity`, `unit_price`, `total_price`) is a continuation line of the preceding item.
     * Merge continuation description text with space, updating the parent line item's bounding box.
2. Feature 18: Multi-Page Table Stitching:
   - Handle invoices spanning multiple pages (e.g. `метро.pdf` 3 pages).
   - Detect table continuation across pages: repeated headers, "Пренос / Продължение", appending rows to the master line items list with page tracking.
3. Feature 19: Occlusion Handling & Strict Null Fallback Policy:
   - Study `капина-03.pdf`: a thermal cash receipt is stapled/occluding the right margin of the invoice page.
   - Design occlusion resilience: when right-hand columns are occluded, do not crash or hallucinate prices.
   - STRICT REQUIREMENT (R3 & R4): NEVER substitute synthetic fallback descriptions like `"Item"`, `"Unknown"`, or `"Placeholder"`. If description text is unresolvable or occluded, assign `description = None` (null) and record a `ValidationIssue(code="MISSING_DESCRIPTION", severity="warning")`.
4. Output:
   Write a comprehensive architectural and algorithmic exploration report to:
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_3/handoff.md
   Notify orchestrator when done.

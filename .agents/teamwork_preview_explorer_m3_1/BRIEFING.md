# BRIEFING — 2026-09-04T22:26:15Z

## Mission
Investigate and design spatial coordinate geometry, token grouping into LogicalLine, and block/zone clustering for Milestone 3 (Features 13 & 14).

## 🔒 My Identity
- Archetype: explorer
- Roles: explorer, synthesizer
- Working directory: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_1
- Original parent: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Milestone: Milestone 3: Spatial Layout Analysis & Table Reconstruction

## 🔒 Key Constraints
- Read-only investigation — do NOT implement project source code
- NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ
- Write only in own directory (.agents/teamwork_preview_explorer_m3_1)

## Current Parent
- Conversation ID: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Updated: 2026-09-04T22:21:30Z

## Investigation State
- **Explored paths**:
  - `invoice_ocr.py` (lines 200–350 dataclasses, 1550–1650 line & block grouping, 1655–1785 table detection, 1900–2010 party extraction, 2850–2950 layout pipeline)
  - `tests/e2e/test_tier1_features.py`, `tests/test_ingestion.py`, `tests/test_challenger_m1_2.py`
  - Real acceptance datasets: `капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`, `метро.pdf` (3 pages)
- **Key findings**:
  - Existing `group_tokens_into_lines` suffers from anchor bias against `current[0]`, fragments tilted lines (e.g. 10 words tilted across page split into 2 lines), and isolates punctuation/superscripts (e.g. period `.` ejected onto standalone line).
  - Vertical overlap metric $V_{\text{intersect}} / \min(h_1, h_2) \ge 0.50$ with adjacent token chaining solves residual skew, superscripts, subscripts, and periods with 100% precision.
  - Page-wide line grouping without column/zone awareness catastrophically merges 2-column party headers (`"Получател ... Доставчик ..."`), causing identical EIK extraction (`207930830`) for both supplier and recipient across Kapina 01, 02, and 03.
  - Physical fiscal slip on `капина-03.pdf` occludes the right side ($x \in [1500, 2300], y \in [400, 1000]$) and bleeds receipt lines into invoice party data without spatial zoning.
  - Multi-page document `метро.pdf` has 3 pages with repeated page headers, repeated table headers, and continued table lines across pages 1, 2, and 3.
  - Defined 7 canonical document spatial zones: Document Header, Supplier Zone, Recipient Zone, Table Body, Financial Summary / Totals, Payment Details, Footer.
- **Unexplored areas**: None remaining for this exploration scope.

## Key Decisions Made
- Established 2D vertical overlap criterion ($V_{\text{intersect}} / \min(h_1, h_2) \ge 0.50$) for line grouping.
- Established `LogicalBlock` structure with backward-compatible sequence protocol (`__iter__`, `__len__`, `__getitem__`).
- Established spatial zone coordinate bounding and dynamic keyword-based party orientation classifier.

## Artifact Index
- DISPATCH.md — Incoming task log
- BRIEFING.md — Working memory
- progress.md — Liveness heartbeat
- handoff.md — Final architectural exploration report

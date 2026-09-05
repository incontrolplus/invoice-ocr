# BRIEFING — 2026-09-04T21:45:00Z

## Mission
Investigate and design multi-pass Tesseract OCR, confidence scoring, token fusion, low-confidence tagging, and test specifications for Milestone 2.

## 🔒 My Identity
- Archetype: explorer
- Roles: investigation, synthesis
- Working directory: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m2_3
- Original parent: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Milestone: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ
- Write only to /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m2_3
- Output comprehensive report to handoff.md

## Current Parent
- Conversation ID: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Updated: 2026-09-04T21:50:00Z

## Investigation State
- **Explored paths**:
  - `ORIGINAL_REQUEST.md`, `PROJECT.md`, M1 worker handoff report
  - `invoice_ocr.py` lines 1-1100, 2350-2450
  - Acceptance PDFs in `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/` (Read-only)
  - Empirical benchmarking scripts: `probe_ocr_passes.py`, `probe_token_overlap.py`, `probe_psm_terms.py`, `probe_all_kapina.py`, `test_fusion_prototype.py`
- **Key findings**:
  1. Preprocessing timing: CLAHE (~7ms), Otsu (~5ms), Grayscale (~0.8ms). Full-image fastNlMeans takes ~1.6s per 300 DPI page and should be omitted or replaced with bilateral filter.
  2. PSM 6 is disastrous on Bulgarian invoice layouts: merges two-column header sections, losing 'доставчик', 'получател', 'еик'.
  3. PSM 3 misses isolated words (e.g. splits 'фактура' into 'Фак'+'а', completely misses 'капина').
  4. PSM 11 yields +22% to +35% tokens, recovers 100% of statutory keywords across all 3 Kapina files, but produces table rule line noise.
  5. Two-Pass Bounding Box Fusion (PSM 3 + PSM 11) with IoU/IoMin clustering and multi-factor scoring boosts mean confidence from ~65% to ~77% and captures 100% (7/7) key Bulgarian statutory terms.
  6. Low-confidence token tagging (`conf < 60`) must be strictly preserved in Layer 1 `raw_ocr_evidence` without being pruned during normalization.
- **Unexplored areas**: None remaining for M2 exploration scope.

## Key Decisions Made
- Select Two-Pass Architecture: Pass 1 (PSM 3) + Pass 2 (PSM 11) with CLAHE preprocessing.
- Reject PSM 6 for page-level OCR due to two-column collapsing.
- Reject expensive fastNlMeans denoising in favor of CLAHE + Otsu/Adaptive threshold.
- Define Spatial Bounding Box Fusion with IoU >= 0.4 or IoMin >= 0.65 matching and multi-factor quality scoring.
- Design Line Noise Filter to eliminate PSM 11 table divider hallucinations.
- Mandate Layer 1 `raw_ocr_evidence` preservation with zero drops for tokens with `conf < 60`.

## Artifact Index
- .agents/teamwork_preview_explorer_m2_3/DISPATCH.md — Received mission parameters and restart recovery
- .agents/teamwork_preview_explorer_m2_3/BRIEFING.md — Persistent working memory and identity
- .agents/teamwork_preview_explorer_m2_3/progress.md — Liveness and task tracking
- .agents/teamwork_preview_explorer_m2_3/probe_ocr_passes.py — Benchmark script for PSM 3, 6, 11
- .agents/teamwork_preview_explorer_m2_3/probe_token_overlap.py — Token overlap and difference analysis
- .agents/teamwork_preview_explorer_m2_3/probe_psm_terms.py — Statutory terms discovery comparison
- .agents/teamwork_preview_explorer_m2_3/probe_all_kapina.py — Multi-file verification script
- .agents/teamwork_preview_explorer_m2_3/test_fusion_prototype.py — Working prototype of Bounding Box Fusion algorithm
- .agents/teamwork_preview_explorer_m2_3/handoff.md — Final 5-component technical report

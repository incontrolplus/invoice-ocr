# BRIEFING — 2026-09-04T21:52:00Z

## Mission
Investigate and design the image enhancement, contrast, binarization, and Cyrillic-preserving noise reduction pipeline for Milestone 2.

## 🔒 My Identity
- Archetype: explorer
- Roles: investigator, synthesizer
- Working directory: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m2_2
- Original parent: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Milestone: Milestone 2 (Adaptive Preprocessing & Multi-Pass OCR)

## 🔒 Key Constraints
- Read-only investigation — do NOT implement in src/
- NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ
- Write all findings and proposals to .agents/teamwork_preview_explorer_m2_2/
- Ensure Bulgarian Cyrillic accents/diacritics and numeric separators are preserved

## Current Parent
- Conversation ID: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Updated: 2026-09-04T21:49:41Z

## Investigation State
- **Explored paths**:
  - `invoice_ocr.py` lines 816–923 (`denoise`, `enhance_contrast`, `adaptive_threshold`, `global_threshold`, `morphological_cleanup`, `generate_preprocessing_variants`)
  - Acceptance dataset: `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`
  - Synthetic diacritic test images (Bulgarian "й", "Й", "ѝ", "è", numbers "12,50", "25.04.2026")
- **Key findings**:
  1. CLAHE on CIELAB L* channel (`clipLimit=2.0, tileGridSize=(8, 8)`) improves faint thermal text confidence from 52.2% to 59.9% in ~81 ms on 300 DPI A4 without color distortions.
  2. Otsu thresholding outperforms Adaptive Gaussian. Adaptive Gaussian causes severe stroke hollowing on bold text and creates >1000 tiny speckles, crashing confidence down to 24-37%.
  3. Fatal polarity bug in existing `morphological_cleanup` (closing on white background erodes black text): erased 52 words on `капина-01.pdf` (including line item quantities and item codes) and mutated decimal commas into periods.
  4. All morphology must be removed from text preprocessing.
  5. Bilateral filter (`d=5, sigmaColor=50, sigmaSpace=50`) is 84x faster than `fastNlMeans` (0.9 ms vs 75.9 ms) and preserves 100% of Cyrillic diacritics and decimal commas.
- **Unexplored areas**: None within scope. Complete handoff written to `handoff.md`.

## Key Decisions Made
- Recommended 2-variant complementary preprocessing: Variant 1 (Deskewed Grayscale) and Variant 2 (LAB L* CLAHE + Bilateral + Otsu).
- Completely eliminated morphological cleanup on text images.
- Replaced `fastNlMeans` with `bilateralFilter`.
- Delivered production-ready interface contracts in `handoff.md`.

## Artifact Index
- DISPATCH.md — Incoming task, server restart update
- BRIEFING.md — Working memory and status
- progress.md — Liveness heartbeat and step tracking
- handoff.md — Final comprehensive technical report with code contracts

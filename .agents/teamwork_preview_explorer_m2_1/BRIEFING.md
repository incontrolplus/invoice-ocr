# BRIEFING — 2026-09-04T21:45:15Z

## Mission
Explore and design OSD orientation detection (Feature 6) and contour-based deskewing (Feature 7) architecture for Milestone 2.

## 🔒 My Identity
- Archetype: explorer
- Roles: explorer, investigator, architect
- Working directory: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m2_1
- Original parent: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Milestone: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR

## 🔒 Key Constraints
- Read-only investigation — do NOT implement in project source code
- NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ
- Write only to your folder /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m2_1

## Current Parent
- Conversation ID: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Updated: 2026-09-04T21:52:00Z

## Investigation State
- **Explored paths**:
  - `invoice_ocr.py` (lines 780-870: OSD & deskewing; lines 2360-2410: pipeline loop)
  - System Tesseract & `.venv`: Tesseract 5.5.2 with `osd`, `bul`, `eng`, `snum` in `/opt/homebrew/share/tessdata/`
  - Kapina acceptance dataset & test PDFs (`капина-01.pdf`, `02`, `03`, `метро.pdf`, `елико.pdf`, `теменужка-01.pdf`, `оскари-01.pdf`)
- **Key findings**:
  1. Tesseract OSD model is installed and functional in `.venv`.
  2. `pytesseract.image_to_osd()` takes ~0.72s on 300 DPI page (3508x2481). Returns `{'rotate': 0|90|180|270, 'orientation_conf': float, ...}`.
  3. Tesseract's `rotate` field indicates clockwise rotation needed to make image upright.
     - `rotate=90` -> `cv2.ROTATE_90_CLOCKWISE`
     - `rotate=180` -> `cv2.ROTATE_180`
     - `rotate=270` -> `cv2.ROTATE_90_COUNTERCLOCKWISE`
  4. On blank/sparse images, `image_to_osd` raises `TesseractError: Too few characters. Skipping this page`. Must be caught and safely defaulted to 0.
  5. `minAreaRect` on raw global point coordinates fails on skew angles near ±10° (detects only 5.7°-8.0°) because the whole-page bounding box resists rotation.
  6. Contour filtering with horizontal morphological dilation (`kernel=(25, 3)`), line aspect ratio >= 2.5, and height 10-60px detects deskew angles with < 0.08° error in 12-15ms. HoughLinesP is accurate but 5x slower (60-75ms).
  7. Border handling: `cv2.BORDER_REPLICATE` smears dark scanner edges across corners; `cv2.BORDER_CONSTANT` with `borderValue=(255, 255, 255)` keeps corners pure white, eliminating spurious OCR noise.
  8. Pipeline architecture: Geometry normalization (OSD + deskew) must occur ONCE per page before multi-variant generation, updating `PageImage` and tracking `PageTransform` for coordinate mapping.
- **Unexplored areas**: None for M2-1. All scope requirements explored and validated.

## Key Decisions Made
- Use 2-Stage Geometry Normalization: Stage 1 = OSD Orientation, Stage 2 = Contour-based Deskewing on upright page.
- Replace whole-page `minAreaRect` with text-line contour filtering + horizontal morphological dilation + median angle.
- Enforce strict safe angle bounds: reject any skew $> 15.0^\circ$ and ignore skews $< 0.2^\circ$.
- Use `cv2.BORDER_CONSTANT` with white border value `(255, 255, 255)` (BGR) or `255` (gray).
- Maintain `PageTransform` contract with forward/inverse affine matrices to track geometry and allow token coordinate recovery.

## Artifact Index
- DISPATCH.md — Dispatch instructions log
- BRIEFING.md — Situational awareness and identity
- progress.md — Liveness heartbeat and milestone tracking
- test_osd_bench.py — OSD benchmark script
- test_deskew_bench.py — Deskew algorithms comparison script
- test_pipeline_integration.py — End-to-end geometry normalization verification script
- handoff.md — Comprehensive technical report and implementation contracts


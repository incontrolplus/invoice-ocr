# BRIEFING — 2026-09-05T01:00:55+03:00

## Mission
Implement Milestone 2 (Adaptive Preprocessing & Multi-Pass OCR Engine) in invoice_ocr.py and create tests in tests/test_preprocessing.py and tests/test_ocr_engine.py.

## 🔒 My Identity
- Archetype: teamwork_preview_worker_m2
- Roles: implementer, qa, specialist
- Working directory: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m2
- Original parent: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Milestone: Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine

## 🔒 Key Constraints
- Write ownership strictly limited to invoice_ocr.py, tests/test_preprocessing.py, tests/test_ocr_engine.py.
- Do NOT touch files in tests/e2e/, test_invoice_ocr.py, or tests/test_ingestion.py unless fixing regressions.
- STRICT READ-ONLY CONSTRAINT: NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ.
- ZERO-DISCARD CONTRACT: Never drop or discard low-confidence tokens from Layer 1 evidence (raw_ocr_evidence).
- Low-confidence tagging (< 60.0) -> is_low_confidence = True.
- No dummy/facade implementations or hardcoded values. Genuine implementation.

## Current Parent
- Conversation ID: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Updated: not yet

## Task Summary
- **What to build**: Orientation detection/normalization, contour deskewing, CLAHE, Cyrillic-safe bilateral denoising & Otsu binarization (fixing morphology bug), multi-pass OCR (PSM 3 + PSM 11), IoU/IoMin bounding box fusion with multi-factor scoring, low-confidence tagging (<60), and comprehensive tests.
- **Success criteria**: All new unit tests pass, existing 29 adversarial tests pass, 15 ingestion tests pass, 55 test_invoice_ocr.py tests pass, 3 Kapina files run cleanly with exit code 0.
- **Interface contracts**: ORIGINAL_REQUEST.md, PROJECT.md
- **Code layout**: invoice_ocr.py, tests/

## Change Tracker
- **Files modified**:
  - `invoice_ocr.py`: Implemented detect_orientation, apply_orientation, detect_deskew_angle, apply_deskew, deskew_image, normalize_page_geometry, enhance_contrast_clahe, denoise_bilateral, binarize_otsu, fixed morphological_cleanup bug, updated generate_preprocessing_variants, execute_ocr_pass, compute_box_metrics, is_line_noise_token, score_token_quality, fuse_ocr_passes, run_multiple_ocr_passes, build_raw_ocr_evidence, and updated PageTransform and Invoice dataclasses.
  - `tests/test_preprocessing.py`: Created comprehensive 26-test suite covering OSD orientation, 90/180/270 rotations, contour deskewing, angle clamping, white border filling, CLAHE luminance enhancement, bilateral denoising, Otsu binarization, Cyrillic diacritic preservation (й, Й, ѝ), and currency decimal comma preservation (12,50).
  - `tests/test_ocr_engine.py`: Created comprehensive 28-test suite covering multi-pass PSM 3 + PSM 11 execution, box overlap metrics (IoU, IoMin), token quality multi-factor scoring, table line noise suppression, split-word resolution, orphan admission, low-confidence tagging (<60), zero-discard policy, and Kapina live acceptance dataset integration.
- **Build status**: PASS (All 54 new M2 tests pass, 44 M1 tests pass, 55 test_invoice_ocr.py tests pass, 3 Kapina acceptance PDFs run with exit code 0)
- **Pending issues**: None

## Quality Status
- **Build/test result**: PASS (54 M2 tests + 44 M1 tests + 55 invoice_ocr tests = 153 passing tests)
- **Lint status**: 0 compilation/syntax errors
- **Tests added/modified**: 54 new tests added in tests/test_preprocessing.py (26) and tests/test_ocr_engine.py (28)

## Loaded Skills
- None specified in dispatch prompt.

## Key Decisions Made
- Executed OSD and deskew geometry normalization once per page in `normalize_page_geometry`, tracking `PageTransform`.
- Substituted slow non-local means with edge-preserving bilateral filtering `denoise_bilateral(d=5, sigmaColor=50, sigmaSpace=50)` reducing latency from 1.6s to < 10ms while preserving Cyrillic diacritics and decimal commas.
- Deactivated morphological background dilation (`morphological_cleanup`) which was previously destroying thin strokes, item codes, and currency decimal commas.
- Implemented multi-pass OCR fusing PSM 3 (layout) and PSM 11 (sparse text) using IoU (>=0.40) and IoMin (>=0.65) matching with multi-factor scoring (keyword, date, monetary, and EIK bonuses, garbage penalties).
- Embedded `raw_ocr_evidence` in `Invoice` output schema conforming to strict Layer 1 zero-discard contract.

## Artifact Index
- DISPATCH.md — Dispatch instructions
- BRIEFING.md — Situational awareness
- progress.md — Liveness & progress tracking
- handoff.md — Milestone 2 completion report

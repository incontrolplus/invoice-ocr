## 2026-09-04T22:01:36Z

You are Challenger 1 for Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine.
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_challenger_m2_1
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md
Worker Handoff: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m2/handoff.md

STRICT CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ.

Task:
Conduct adversarial stress-testing of Milestone 2 preprocessing and OCR pipelines:
1. Author and execute adversarial test harness (using tmp_path):
   - Extreme rotation: test images rotated 45°, 90°, 135°, 180°, 270°, 360°. Verify OSD behavior and that angles outside 90/180/270 do not corrupt orientation.
   - Extreme skew: test angles at boundary (-15.0°, +15.0°), just outside (-15.1°, +16.0°), and extreme (±45°, ±85°). Verify that extreme skews are safely rejected (returned as 0.0) without 90° flipping.
   - Degraded / inverted scans: pure black images, pure white images, single-pixel images, checkerboard images, inverted color images (white text on dark background). Verify that `detect_orientation` and `detect_deskew_angle` never crash or throw unhandled exceptions.
   - Bulgarian Cyrillic diacritic stress test: verify that characters "й", "Й", "ѝ", "è", and numbers with decimal commas "12,50", "0,20", "1.95583" are NOT erased, blurred, or corrupted by preprocessing variants.
   - Line noise stress test: test table border strings `----`, `____`, `====`, `|` to ensure `is_line_noise_token` correctly suppresses them during fusion without dropping legitimate words.
2. Verify zero modifications to `/Volumes/NO NAME/_ФАКТУРИ`.
3. Render verdict: APPROVE or REQUEST_CHANGES.
Write report to:
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_challenger_m2_1/handoff.md
Notify orchestrator when done.

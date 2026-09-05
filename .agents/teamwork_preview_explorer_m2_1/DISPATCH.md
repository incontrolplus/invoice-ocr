## 2026-09-04T21:45:03Z

You are Explorer 1 for Milestone 2: Adaptive Preprocessing & Multi-Pass OCR.
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m2_1
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Architecture: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md

Prior survey & handoff reports to read:
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_spec_miner_survey_1/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_2/handoff.md
- /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m1_iter2/handoff.md

STRICT CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ.

Task:
Explore and design the OSD orientation detection and contour-based deskewing architecture:
1. OSD Orientation Detection (Feature 6):
   - Test `pytesseract.image_to_osd()` with `--psm 0 -l osd` (or fallback). Check if Tesseract OSD model is installed and works on `.venv`.
   - Measure execution speed and reliability on rotated invoice pages (90°, 180°, 270°).
   - Design graceful fallback if OSD confidence is low or fails on small/sparse images.
   - Design rotation transform using `cv2.rotate` (e.g. `ROTATE_90_CLOCKWISE`, `ROTATE_180`, `ROTATE_90_COUNTERCLOCKWISE`).
2. Contour-based Deskewing (Feature 7):
   - Investigate deskewing algorithms: Hough line transform vs `cv2.minAreaRect` on thresholded text contours.
   - Determine safe angle bounds (e.g. [-15.0°, 15.0°]) to prevent erroneous 90° flips.
   - Design affine rotation matrix with border replication or white background filling (`borderValue=(255, 255, 255)`).
   - Ensure coordinate mapping or rotation tracking so downstream bounding boxes remain consistent.
3. Formulate concrete implementation recommendations and interface contracts for the Worker.

Output:
Write a comprehensive technical report to:
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m2_1/handoff.md

## 2026-09-04T21:49:39Z

**Context**: Server restart recovery for Milestone 2 Exploration (OSD Orientation & Deskewing)
**Content**: The server was restarted. Please resume your exploration from your current state in `progress.md`. Continue testing `pytesseract.image_to_osd()`, contour deskewing (`cv2.minAreaRect`), safe angle bounds, and border handling. Synthesize your findings into `handoff.md`.
**Action**: Resume execution, write `handoff.md`, and send a completion message to the orchestrator.

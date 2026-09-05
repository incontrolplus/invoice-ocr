# Orchestrator Soft Handoff — State Dump for Successor Orchestrator (Generation 2)

## 1. Overview & Identity
- **Current Orchestrator**: Generation 1 (`orchestrator_1`)
- **Parent Conversation ID**: `bb1f68a2-1d32-4d3c-86f4-7773b2ea4d61`
- **Project Root**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr`
- **Successor Working Directory**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/orchestrator_gen2` (or working directory configured for gen2)
- **Hard Constraints**:
  - Strict Read-Only Policy: NEVER modify, move, or delete any files in `/Volumes/NO NAME/_ФАКТУРИ`.
  - Dispatch-Only: NEVER write source code or run build/test commands directly. Delegate all tasks via `invoke_subagent`.
  - Forensic Audit Veto: Auditor verdict is a non-negotiable binary veto.

---

## 2. Milestone State

| # | Milestone Name | Status | Verified Deliverables & Test Status |
|---|----------------|--------|-----------------------------------|
| Survey | Requirements, Codebase & Dataset Survey | **DONE** | 3 survey reports aggregated into `PROJECT.md` Feature Inventory (47 features). |
| M_E2E | E2E Testing Track | **DONE** | Published `TEST_INFRA.md`, 89 tests across Tiers 1-4 in `tests/e2e/`, `run_e2e_tests.py`, and `TEST_READY.md`. |
| M1 | Multi-Format Ingestion | **DONE** | Gate PASSED. PyMuPDF 300 DPI rasterization, BGR C-contiguous arrays, CMYK conversion, positive DPI validation, safe exception translation, page tracking on `OcrToken`, `LogicalLine` interface contract. 100% pass on 29 adversarial tests, 15 ingestion tests, 55 unit regression tests. External volume verified 100% untouched. |
| M2 | Adaptive Preprocessing & Multi-Pass OCR | **PLANNED** | Ready for immediate execution by Successor. |
| M3 | Spatial Layout & Table Reconstruction | **PLANNED** | Block/line clustering, column synonym matching, multi-line item merging, null fallbacks for thermal slip occlusions. |
| M4 | Deterministic Field Extraction & Tax Rules | **PLANNED** | EIK Mod-11, IBAN Mod-97, VAT ID, Decimal currency objects, leap-year date validation. |
| M5 | Financial Validation & Euro Engine | **PLANNED** | Dual currency parity (1.95583), 0.01/0.02 tolerances, 2026 Euro transition rules, 3-layer architecture serialization. |
| M6 | CLI, Batch & Debug Artifacts | **PLANNED** | Single-file JSON stdout / stderr logs, `--input-dir`, `--output-dir`, `batch_summary.json`, `--debug`. |
| M7 | Final Acceptance & Adversarial Hardening | **PLANNED** | 100% pass on E2E test suite (Tiers 1-4), 3 Kapina acceptance files, zero-touch verification, Tier 5 adversarial coverage hardening. |

---

## 3. Active Subagents
- **All 19 subagents spawned by Generation 1 have concluded and delivered their handoffs.**
- Total spawn count: 19 / 16 (threshold reached).
- Currently active running subagents: **0**.

---

## 4. Pending Decisions & Technical Context for Successor

### 4.1 Milestone 2 Scope & Objectives
Milestone 2 addresses Features 6–12 in `PROJECT.md`:
- **OSD Orientation Correction**: Fast orientation detection using Tesseract OSD (`--psm 0 -l osd` or lightweight heuristic) to rotate 90/180/270 degrees before OCR.
- **Contour-based Deskewing**: Compute skew angle via Hough lines or minAreaRect on text contours, rotate within sensible bounds (e.g. [-15°, 15°]) using `cv2.warpAffine` without clipping.
- **Image Enhancement Pipeline**:
  - CLAHE (Contrast Limited Adaptive Histogram Equalization) on luminance channel for low-contrast/faded scans.
  - Adaptive thresholding (`cv2.adaptiveThreshold`) and Otsu binarization (`cv2.threshold(..., cv2.THRESH_OTSU)`).
  - Noise reduction (bilateral filter or morphological opening) to suppress scan speckles without eroding Bulgarian Cyrillic characters (e.g. "й", "ь", small dots).
- **Multi-Pass OCR Engine**:
  - Pass 1: Standard layout analysis (PSM 3) with Bulgarian language pack (`lang="bul"`).
  - Pass 2: Sparse text / table-oriented pass (PSM 11 or PSM 6) to catch isolated numbers, table cells, and stamps.
  - OCR Pass Scoring & Fusion: Merge or score passes by confidence, word length, and dictionary matches; deduplicate overlapping bounding boxes (IoU > 0.6).
  - Low-Confidence Tagging: Every token with confidence `< 60` must have `is_low_confidence=True`, retained in evidence without discarding.

### 4.2 Documented Escaped Defects to Keep in Mind (to be fixed in M3-M6)
Documented in `TEST_READY.md`:
1. `TypeError` in `_validate_totals`: `items_sum = sum(item.total_price_net...)` adds `int` to `MoneyAmount` (Milestone M5).
2. Substring collision in `_match_column_synonym`: `"ед"` in `unit` matches before `"ед. цена"` in `unit_price` (Milestone M3).
3. Missing calendar leap year validation in `parse_date("29.02.2025")` (Milestone M4).
4. Missing CLI batch (`--input-dir`, `--output-dir`) and debug (`--debug`, `--debug-dir`) arguments in `main()` (Milestone M6).

---

## 5. Concrete Remaining Work & Next Steps for Successor

1. **Initialize Working Directory**:
   - Create `.agents/orchestrator_gen2/`
   - Set up `BRIEFING.md`, `progress.md`, and copy parent conversation ID `bb1f68a2-1d32-4d3c-86f4-7773b2ea4d61`.
   - Start fresh heartbeat cron: `schedule(CronExpression="*/10 * * * *")`.
2. **Execute Milestone 2**:
   - Follow standard iteration pattern (2B):
     - a. Spawn 3 Explorers (e.g. Explorer 1: OSD & Deskewing; Explorer 2: CLAHE & Adaptive Binarization; Explorer 3: Multi-Pass Tesseract PSM 3/11 & Confidence Scoring).
     - b. Spawn 1 Worker with exclusive write ownership of `invoice_ocr.py` and `tests/test_preprocessing.py`.
     - c. Spawn 2 Reviewers.
     - d. Spawn 2 Challengers.
     - e. Spawn 1 Forensic Auditor.
     - f. Gate evaluation in `GATE_STATUS.md`.
3. **Advance Milestones M3 through M7**:
   - Proceed sequentially through M3 (Layout & Tables) -> M4 (Tax Rules & Mod Checksums) -> M5 (Financial & Euro Engine) -> M6 (CLI Batch & Debug) -> M7 (100% E2E Pass & Acceptance Dataset Verification).
4. **Final Victory Claim**:
   - Send complete report to user and parent claiming victory for post-victory audit.

---

## 6. Key Artifacts Index
- `ORIGINAL_REQUEST.md`: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md`
- `PROJECT.md`: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md`
- `TEST_INFRA.md`: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/TEST_INFRA.md`
- `TEST_READY.md`: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/TEST_READY.md`
- `GATE_STATUS.md` (M1): `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/orchestrator_1/GATE_STATUS.md`
- `progress.md`: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/orchestrator_1/progress.md`
- `BRIEFING.md`: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/orchestrator_1/BRIEFING.md`
- Acceptance dataset: `/Volumes/NO NAME/_ФАКТУРИ` (STRICT READ-ONLY)

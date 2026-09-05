# Original User Request

## Initial Request — 2026-09-04T21:13:53Z

Production-ready Bulgarian Invoice OCR & Document Understanding pipeline supporting PDF and image inputs, layout/table reconstruction, deterministic field extraction, strict Bulgarian euro/BGN & VAT validation, and batch regression testing against real-world test invoices.

Working directory: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr`
Integrity mode: benchmark

## Verification Resources
- Primary Acceptance Dataset (READ-ONLY):
  - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf`
  - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf`
  - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf`
- Parent dataset folder containing real invoices for batch validation (READ-ONLY):
  - `/Volumes/NO NAME/_ФАКТУРИ`
- Existing codebase to audit, upgrade and preserve functioning components:
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py`
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/test_invoice_ocr.py`
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv`

## Requirements

### R1. Multi-Format & Multi-Page Ingestion
Support `.png`, `.jpg`, `.jpeg`, and `.pdf` files.
- Detect whether input is an image or PDF.
- For PDFs, rasterize pages using PyMuPDF (fitz) at 300-400 DPI.
- Track `page_number` and bounding box per OCR token/evidence across all pages.
- Detect page boundaries, line item continuation across pages, and totals/summary page.
- Never modify, move, or delete any source files in `/Volumes/NO NAME/_ФАКТУРИ`.

### R2. Adaptive Preprocessing & Multi-Pass OCR Engine
- Ensure Python environment has required libraries installed (`pymupdf`, `opencv-python`, `pillow`, `pytesseract`, `numpy`).
- Preprocessing pipeline: orientation detection (OSD), deskewing (contour-based), noise reduction, contrast enhancement (CLAHE), adaptive/Otsu binarization.
- Multi-pass OCR using Tesseract with `lang="bul"` under both PSM 3 and PSM 11.
- Select the best OCR pass using confidence and character count scoring.
- Mark tokens with confidence < 60 as low-confidence evidence without arbitrarily discarding them.

### R3. Coordinate-Based Layout Analysis & Table Reconstruction
- Group tokens geometrically into lines and blocks by spatial coordinates (X, Y, width, height) rather than trusting raw reading order.
- Detect table header regions by matching Bulgarian column synonyms (№, описание, количество, мярка, ед. цена, стойност, ДДС).
- Map data rows to columns based on horizontal alignment.
- Never substitute synthetic fallback descriptions such as `"Item"`; if unresolvable, assign `null` and record a validation warning.

### R4. Deterministic Field Extraction & Bulgarian Tax Rules
- Extract metadata (invoice number, date issued, date tax event, place of issue).
- Distinguish between Supplier and Recipient parties using semantic keywords (Доставчик vs Получател) and document spatial zones.
- Extract EIK (9/13 digits) and VAT numbers (`BG` prefix + digits).
- Extract payment details (bank, IBAN matching Bulgarian format, BIC).
- Parse all monetary values into Python `Decimal` objects with explicit currency (`{ "amount": ..., "currency": ... }`).
- Capture amount-in-words text as raw textual evidence without treating it as source of truth.

### R5. Rigorous Financial, Euro-Transition & Anomaly Validation
- Mathematically verify:
  1. `sum(line_items.total_price_net) == financial_summary.tax_base` (tolerance 0.01/0.02)
  2. `tax_base * vat_rate == vat_amount`
  3. `tax_base + vat_amount == total_amount_due`
- Handle Bulgarian Euro Transition (2026):
  - If date >= 2026-01-01 and currency is BGN, flag potential mismatch warning; do NOT auto-convert BGN to EUR.
  - If date >= 2026-08-08 (post dual-display deadline), flag BGN as primary payable currency.
  - Cross-check amount-in-words currency against numeric currency without modifying extracted numbers.
- Maintain a strict three-layer separation: (1) Raw OCR Evidence, (2) Normalized Data, (3) Validation Results (`is_valid`, `errors`, `warnings`).
- Never fabricate missing fields to force a document to appear valid.

### R6. CLI, Batch Processing & Debug Artifacts
- Single-file mode: prints strictly valid JSON to stdout; all logging and diagnostic output directed to stderr.
- Batch mode (`--input-dir`): recursively scans directory, executes on each document independently, saves JSONs to `--output-dir` (default `results/`), and outputs `batch_summary.json`.
- Debug mode (`--debug`): writes intermediate artifacts (rendered pages, preprocessed images, token maps, layout trees) to `--debug-dir` (default `debug/`).

## Acceptance Criteria

### Automated Verification
- [ ] PyMuPDF dependency is installed and operational in `.venv`.
- [ ] 100% pass on comprehensive unit tests covering `parse_money()`, `normalize_eik()`, `normalize_vat_number()`, `parse_date()`, `clean_ocr_artifacts()`, `normalize_iban()`, and financial validation formulas.

### Real-World Dataset Acceptance (Kapina Invoices)
- [ ] Successfully execute and produce structured JSON for the 3 mandatory real files:
  - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf`
  - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf`
  - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf`
- [ ] No crashes, unhandled exceptions, or file lock issues during PDF rasterization and OCR.
- [ ] Generate a diagnostic verification table for the 3 test invoices detailing: File, Pages, Confidence, Line items count, Tax base, VAT, Total, Currency, and Validation status.
- [ ] Verify that zero files in `/Volumes/NO NAME/_ФАКТУРИ` were modified, deleted, or overwritten.

### Schema & Production Standards
- [ ] Output strictly conforms to the specified JSON schema with explicit currency objects on all monetary fields (`{ "amount": ..., "currency": ... }`).
- [ ] Single-file execution stdout contains ONLY the JSON payload, leaving stderr for all diagnostic logs.
- [ ] Missing or unresolvable values serialize as `null` with machine-readable validation issues (`code`, `message`, `severity`).

## Follow-up — 2026-09-04T21:48:54Z

The server was restarted, and background tasks were stopped. The user has explicitly requested to continue. Please revive/check the orchestrator and all active tasks, verify the current milestone status, and proceed with Milestone 2 (Adaptive Preprocessing & Multi-Pass OCR Engine) and subsequent milestones as planned.

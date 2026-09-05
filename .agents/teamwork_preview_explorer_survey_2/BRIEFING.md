# BRIEFING — 2026-09-04T21:16:50Z

## Mission
Conduct an in-depth survey of the invoice OCR codebase, Python environment, Tesseract setup, and architectural gaps against requirements R1-R6.

## 🔒 My Identity
- Archetype: teamwork_preview_explorer
- Roles: Survey Agent 2, Explorer, Synthesizer
- Working directory: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_2
- Original parent: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Milestone: Milestone 1 - Architectural Survey & Environment Assessment

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Write only to own folder (.agents/teamwork_preview_explorer_survey_2)
- Produce 5-component handoff report (Observation, Logic Chain, Caveats, Conclusion, Verification Method) in handoff.md
- Maintain progress.md heartbeat

## Current Parent
- Conversation ID: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Updated: 2026-09-04T21:16:50Z

## Investigation State
- **Explored paths**:
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py` (2275 lines)
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/test_invoice_ocr.py` (201 lines)
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv`
  - System Tesseract installation (`/opt/homebrew/bin/tesseract`)
  - Target dataset: `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/`
- **Key findings**:
  - Python version is 3.14.7.
  - `.venv` contains `opencv-python 5.0.0.93`, `pillow 12.3.0`, `pytesseract 0.3.13`, `numpy 2.5.2`.
  - PyMuPDF (`fitz`) and `pytest` are NOT installed in `.venv`, but wheel installation succeeds cleanly.
  - Tesseract 5.5.2 is installed at `/opt/homebrew/bin/tesseract` with `bul`, `osd`, `eng`, `snum` in `/opt/homebrew/share/tessdata/`.
  - `invoice_ocr.py` completely lacks PDF ingestion (crashes with "Unsupported file type: .pdf").
  - `OcrToken` and layout model lack `page_number` tracking and confidence < 60 tagging.
  - `normalize_eik` lacks Bulgarian Mod-11 checksum validation.
  - `normalize_iban` lacks ISO 7064 Mod-97 checksum validation.
  - LineItem monetary fields lack explicit currency objects (`{ "amount": ..., "currency": ... }`).
  - Validation totals contain a tautological bug in VAT rate inference.
  - Batch mode (`--input-dir`, `--output-dir`, `batch_summary.json`) and Debug mode (`--debug`, `--debug-dir`) are completely missing from CLI.
  - Test suite passes 55/55 existing unit tests, but lacks financial validation formulas, EIK checksum, and pytest integration.
- **Unexplored areas**: None within survey scope.

## Key Decisions Made
- Confirmed full read-only compliance (no files modified outside `.agents/teamwork_preview_explorer_survey_2`).
- Detailed gap matrix mapped across R1-R6 for handoff.

## Artifact Index
- DISPATCH.md — Initial dispatch instructions
- BRIEFING.md — Persistent working memory
- progress.md — Liveness heartbeat and step tracking
- handoff.md — Comprehensive 5-component report

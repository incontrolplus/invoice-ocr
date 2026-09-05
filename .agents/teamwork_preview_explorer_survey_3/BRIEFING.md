# BRIEFING — 2026-09-04T21:22:00Z

## Mission
Inspect and analyze the acceptance test dataset (3 primary acceptance files and broader directory) strictly read-only.

## 🔒 My Identity
- Archetype: teamwork_preview_explorer
- Roles: explorer, synthesizer
- Working directory: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_3
- Original parent: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Milestone: Acceptance Dataset Survey (Survey 3)

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- NEVER modify, move, or delete any source files in `/Volumes/NO NAME/_ФАКТУРИ`
- Do not write any temporary files in `/Volumes/NO NAME/_ФАКТУРИ`
- Write only to own directory: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_3`

## Current Parent
- Conversation ID: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Updated: 2026-09-04T21:15:12Z

## Investigation State
- **Explored paths**:
  - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf`
  - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf`
  - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf`
  - All 12 supplier directories and 23 total files in `/Volumes/NO NAME/_ФАКТУРИ`
- **Key findings**:
  - All 3 primary acceptance files are 1200 DPI scans (9920x14030 px) created with NAPS2 via PDFsharp; contain flawed invisible OCR text layers.
  - Page rendering at 300 DPI via PyMuPDF is essential for production performance and memory safety.
  - Invoices have dual currency: table unit prices and line totals are printed in EUR (€), summary block displays both BGN and EUR, amount in words is in EUR.
  - `капина-03.pdf` has a physical fiscal receipt stapled over columns 5-7 for lines 1-12 and over parts of the supplier box.
  - The broader dataset comprises 23 PDF files across 12 supplier folders; 21 single-page and 2 multi-page files (Metro).
- **Unexplored areas**: None within the survey scope; complete census accomplished.

## Key Decisions Made
- Confirmed strict read-only adherence on `/Volumes/NO NAME/_ФАКТУРИ` (zero file modifications).
- Recommended downsampling 1200 DPI PDFs to 300 DPI in PyMuPDF for Tesseract OCR pipeline.
- Established that NAPS2 embedded text layer must not be trusted; genuine multi-pass OCR is required.
- Verified exact mathematical formulas and dual-currency mapping for all 3 Kapina acceptance files.

## Artifact Index
- DISPATCH.md — Initial dispatch instructions
- progress.md — Liveness heartbeat and task progress
- handoff.md — Comprehensive survey report

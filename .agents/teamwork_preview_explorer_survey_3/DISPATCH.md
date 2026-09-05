## 2026-09-04T21:15:12Z

You are Survey Agent 3 (teamwork_preview_explorer).
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_3
Authoritative Requirements file: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md

You MUST read /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md before starting work.

STRICT READ-ONLY CONSTRAINT:
NEVER modify, move, or delete any source files in `/Volumes/NO NAME/_ФАКТУРИ`. Do not write any temporary files there. Treat this directory and all its files as strictly read-only.

Task:
Inspect and analyze the acceptance test dataset:
1. Examine the 3 primary acceptance files:
   - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf`
   - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf`
   - `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf`
   For each file:
   - Determine number of pages, dimensions, resolution.
   - Determine if pages have embedded digital text streams or are purely scanned raster images.
   - Inspect visual layout, rotation/skew, image quality, contrast, artifacts.
   - Inspect document content: supplier name/EIK/VAT, recipient name/EIK/VAT, invoice number, date, tax event date, currency (BGN / EUR / dual), payment details (IBAN, bank), table columns and headers, number of line items, tax base, VAT rate, VAT amount, total amount.
   - Note any specific layout quirks, fonts, multi-line table rows, or challenging OCR regions.
2. Survey the broader `/Volumes/NO NAME/_ФАКТУРИ` directory:
   - Directory structure, file naming patterns, total file count, formats (.pdf, .jpg, .png).
   - Variations across different suppliers/issuers (e.g. are there other folders besides Капина 71 ООД?).

Output:
Write a comprehensive handoff report to:
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_3/handoff.md
Update progress.md as you work. When finished, send a message to orchestrator.
